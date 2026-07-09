import asyncio, time, subprocess, httpx, json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import uvicorn
from typing import Literal

WINDOWS_MAC = 'A0:AD:9F:B1:E5:DB' 
WINDOWS_IP = '192.168.42.42'
WINDOWS_USER = 'Karl'
LLAMA_PORT = '8080'
LLAMA_URL = f'http://{WINDOWS_IP}:{LLAMA_PORT}'

TIMEOUT = 60
TIMEOUT_COUNTER = 0

BOOT_LOCK: asyncio.Lock = None



app = FastAPI()
HTTP_CLIENT: httpx.AsyncClient = None
LAST_REQUEST_TIME = time.time()
PC_STATE: Literal['unknown', 'ready', 'starting', 'do-not-disturb', 'off'] = 'unknown'
LOADED_MODEL: str | None = None

async def send_wol(): await asyncio.create_subprocess_exec("wakeonlan", WINDOWS_MAC)


async def ssh_run(cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        'ssh', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=no',
        f"{WINDOWS_USER}@{WINDOWS_IP}", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()



async def start_llama(): 
    global PC_STATE

    await ssh_run('powershell -c net start LlamaServer')
    print(f'[STARTING LLAMA] llama.cpp routing server successfully started')


async def stop_llama(): 
    global LOADED_MODEL, PC_STATE
    
    await ssh_run('powershell -c net stop LlamaServer')

    PC_STATE = 'unknown'
    LOADED_MODEL = None
    print(f'[STOPPING LLAMA] llama.cpp routing server successfully stopped')



async def load_model(model_id: str) -> bool:
    global LOADED_MODEL
    print(f'[MODEL LOADING] Loading model {model_id}')

    try:
        warmup_prompt = dict(
            model = model_id,
            prompt = '',
            max_tokens = 1,
            stream = False
        )
        result = await HTTP_CLIENT.post(f'{LLAMA_URL}/v1/completions', json=warmup_prompt, timeout=httpx.Timeout(100))
        if result.status_code == 200:
            print(f'[MODEL LOADING] Model {model_id} successfully loaded')
            LOADED_MODEL = model_id
            return True
        else:
            print(f'[MODEL LOADING ERROR] Unable to load model {model_id}: Failed to process warm-up')
    except Exception as e:
        print(f'[MODEL LOADING ERROR] Warm-up failed: {e}')
        return False
    return False


async def unload_model():
    global LOADED_MODEL, PC_STATE

    try:
        result = await HTTP_CLIENT.post(f'{LLAMA_URL}/models/unload', json={'model': LOADED_MODEL})
        if result.status_code == 200:
            print(f'[MODEL UNLOADING] Model {LOADED_MODEL} successfully unloaded')
            LOADED_MODEL = None
            return
    except Exception as e:
        print(f'[MODEL UNLOADING ERROR] Failed to unload model: {e}')






async def shutdown_if_idle():
    global TIMEOUT_COUNTER
    global PC_STATE

    while True:
        await asyncio.sleep(60)
        if time.time() - LAST_REQUEST_TIME > TIMEOUT and PC_STATE == 'ready':
                if TIMEOUT_COUNTER >= 20: 
                    print(f'[TIMEOUT TRACKER] No usage detected, shutting down inference machine')
                    TIMEOUT_COUNTER = 0
                    PC_STATE = 'off'
                    await stop_llama()
                    await ssh_run("shutdown /s /t 60")
                elif TIMEOUT_COUNTER >= 10:
                    print(f'[TIMEOUT TRACKER] No usage detected, shutting down llama.cpp server')
                    PC_STATE = 'unknown'
                    await stop_llama()
                elif TIMEOUT_COUNTER >= 5:
                    print(f'[TIMEOUT TRACKER] No usage detected, unloading current model {LOADED_MODEL}')
                    PC_STATE = 'unknown'
                    await unload_model()
                print(f'[TIMEOUT TRACKER] Detected timeout period: {TIMEOUT - TIMEOUT_COUNTER}/{TIMEOUT}')
                TIMEOUT_COUNTER += 1
        else: TIMEOUT_COUNTER = 0



async def is_dnd_active() -> bool:
    try:
        result = await ssh_run('powershell -c "Test-Path C:\\Users\\Karl\\.llama-proxy\\llama-dnd.flag"')
        return result.strip().lower() == "true"
    except Exception:
        return False


async def check_avaibility():
    global PC_STATE

    while True:
        await asyncio.sleep(30)
        if not await is_pc_reachable():
            if PC_STATE != 'starting': PC_STATE = 'off'
            continue
        if await is_dnd_active():
            if PC_STATE == 'ready': 
                print(f'[DO-NOT-DISTURB] Do-not-disturb enabled')
                await stop_llama()
            PC_STATE = 'do-not-disturb'
        elif PC_STATE == 'do-not-disturb':
            PC_STATE = 'unknown'



@app.on_event("startup")
async def startup():
    print('[SERVICE] Starting up Service')
    global BOOT_LOCK, HTTP_CLIENT

    BOOT_LOCK = asyncio.Lock()
    HTTP_CLIENT = httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=10, 
            max_keepalive_connections=0  
        )
    )
    asyncio.create_task(shutdown_if_idle())
    asyncio.create_task(check_avaibility())



@app.on_event("shutdown")
async def shutdown():
    print(f'[SERVICE] Shutting off Service')

    await stop_llama()
    await HTTP_CLIENT.aclose()





async def is_pc_reachable() -> bool:
    proc = await asyncio.create_subprocess_exec(
        'ping', '-c', '1', '-W', '2', WINDOWS_IP,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL
    )
    return await proc.wait() == 0



async def is_llama_running() -> bool:
    print(f'[CHECK LLAMA] Checking if llama.cpp server is running')

    try:
        result = await HTTP_CLIENT.get(f"{LLAMA_URL}/health", timeout=2)
        if result.status_code == 200:
            print(f'[CHECK LLAMA] llama.cpp server is running')
            return True
        else: 
            print(f'[CHECK LLAMA ERROR] llama.cpp server is not running')
            return False
    except Exception:
        print(f'[CHECK LLAMA] llama.cpp server is not responding, assuming off')
        return False
    


async def is_model_loaded(model_id: str) -> bool:
    global LOADED_MODEL
    print(f'[CHECK MODEL] Checking which models are loaded')

    try:
        result = await HTTP_CLIENT.get(f'{LLAMA_URL}/models')
        if result.status_code != 200:  return False

        models = result.json().get('data', [])
        for model in models:
            if model.get('status', {}).get('value') == 'loaded' and model.get('id') == model_id:
                print(f'[CHECK MODEL] Affirmed that model {model.get("id")} is loaded ')
                return True
        else:
            print(f'[CHECK MODEL] Model {model_id} is not loaded!')
            return False
        
    except Exception: 
        print(f'[CHECK MODEL ERROR] Unable to reach endpoint /models!')
        return False



async def ensure_inference_ready(model_id: str):
    global PC_STATE, LOADED_MODEL

    if model_id is not None and model_id != LOADED_MODEL and LOADED_MODEL is not None: 
        raise RuntimeError(f"[MODEL LOADING ERROR] Can't use model {model_id} when {LOADED_MODEL} is active")

    print(f'[PREPARE] Checking and Starting Inference Engine')
    print(f'[PREPARE] Checking and Starting Inference Engine')
    if PC_STATE == 'ready' and (model_id is None or model_id == LOADED_MODEL): return
    if PC_STATE == 'starting': raise RuntimeError('[PREPARE ERROR] Another client is already starting the Inference Machine, please wait')
    if PC_STATE == 'do-not-disturb': raise RuntimeError("[DO-NOT-DISTURB] Request blocked by owner")    

    async with BOOT_LOCK:
        if model_id is not None and model_id != LOADED_MODEL and LOADED_MODEL is not None: 
            raise RuntimeError(f"[MODEL LOADING ERROR] Can't use model {model_id} when {LOADED_MODEL} is active")
        if PC_STATE == 'ready' and (model_id is None or model_id == LOADED_MODEL): return
        if PC_STATE == 'starting': raise RuntimeError('[PREPARE ERROR] Another client is already starting the Inference Machine, please wait')
        if PC_STATE == 'do-not-disturb': raise RuntimeError("[DO-NOT-DISTURB] Request blocked by owner")

        if not await is_pc_reachable():
            print(f'[PREPARE] Booting up Inference Machien')
            PC_STATE = 'starting'
            await send_wol()
            for _ in range(60):
                await asyncio.sleep(2)
                if await is_pc_reachable():
                    break
            else:
                PC_STATE = 'off'
                raise RuntimeError("[BOOTING ERROR] Unable to boot Inference Machine up in time, WakeOnLan unsuccessful")
            print('[PREPARE] Successfully booted Inference Machien')
            await asyncio.sleep(5)


        if not await is_llama_running():
            print(f'[PREPARE] Starting llama router service')
            PC_STATE = 'starting'
            await start_llama()
            for _ in range(30):
                await asyncio.sleep(2)
                try:
                    if await is_llama_running(): break
                except Exception: pass
            else: 
                PC_STATE = 'unknown'
                raise RuntimeError("[LLAMA ERROR] Unable to boot llama.cpp up in time")
            await asyncio.sleep(5)

        
        if model_id is not None:
            if not await is_model_loaded(model_id):
                print(f'[PREPARING] Loading model {model_id}')
                PC_STATE = 'starting'
                await load_model(model_id)
                for _ in range(60):
                    await asyncio.sleep(2)
                    try:
                        if await is_model_loaded(model_id): break
                    except Exception: pass
                else:
                    PC_STATE = 'unknown'
                    raise RuntimeError('[MODEL LOADING ERRRO] Unable to load model in time')
                await asyncio.sleep(5)


        print(f'[PREPARE] Inferene machine successfully started')
        PC_STATE = 'ready'




@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy(request: Request, path: str):
    print('[SERVICE] Received request for inference')
    
    global LAST_REQUEST_TIME
    LAST_REQUEST_TIME = time.time()

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("accept-encoding", None)

    model_id = None
    if request.method == "POST" and body:
        try:
            data = json.loads(body)
            model_id = data.get("model")
        except: pass



    try:
        await ensure_inference_ready(model_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    if PC_STATE != 'ready': raise HTTPException(status_code=503, detail="[PREPARING INFERENCE MACHINE ERROR] Unable to start llama-server!")


    req = HTTP_CLIENT.build_request(
        method=request.method,
        url=f"http://{WINDOWS_IP}:{LLAMA_PORT}/{path}",
        headers=headers,
        content=body,
    )
    resp = await HTTP_CLIENT.send(req, stream=True)

    async def body_iterator():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    response_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in (
            "content-length",
            "transfer-encoding",
            "connection",
        )
    }

    print('[SERVICE] Successfully processed request')

    return StreamingResponse(
        body_iterator(),
        status_code=resp.status_code,
        headers=response_headers,
        media_type=resp.headers.get("content-type"),
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9090)