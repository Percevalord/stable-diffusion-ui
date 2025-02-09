"""runtime.py: torch device owned by a thread.
Notes:
    Avoid device switching, transfering all models will get too complex.
    To use a diffrent device signal the current render device to exit
    And then start a new clean thread for the new device.
"""
import json
import os, re
import traceback
import torch
import numpy as np
from gc import collect as gc_collect
from omegaconf import OmegaConf
from PIL import Image, ImageOps
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange
import time
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import nullcontext
from einops import rearrange, repeat
from ldm.util import instantiate_from_config
from optimizedSD.optimUtils import split_weighted_subprompts
from transformers import logging

from gfpgan import GFPGANer
from basicsr.archs.rrdbnet_arch import RRDBNet
from realesrgan import RealESRGANer

import uuid

logging.set_verbosity_error()

# consts
config_yaml = "optimizedSD/v1-inference.yaml"
filename_regex = re.compile('[^a-zA-Z0-9]')

# api stuff
from . import Request, Response, Image as ResponseImage
import base64
from io import BytesIO
#from colorama import Fore

from threading import local as LocalThreadVars
thread_data = LocalThreadVars()

def get_processor_name():
    try:
        import platform, subprocess
        if platform.system() == "Windows":
            return platform.processor()
        elif platform.system() == "Darwin":
            os.environ['PATH'] = os.environ['PATH'] + os.pathsep + '/usr/sbin'
            command ="sysctl -n machdep.cpu.brand_string"
            return subprocess.check_output(command).strip()
        elif platform.system() == "Linux":
            command = "cat /proc/cpuinfo"
            all_info = subprocess.check_output(command, shell=True).decode().strip()
            for line in all_info.split("\n"):
                if "model name" in line:
                    return re.sub( ".*model name.*:", "", line,1).strip()
    except:
        print(traceback.format_exc())
        return "cpu"

def device_would_fail(device):
    if device == 'cpu': return None
    # Returns None when no issues found, otherwise returns the detected error str.
    # Memory check
    try:
        mem_free, mem_total = torch.cuda.mem_get_info(device)
        mem_total /= float(10**9)
        if mem_total < 3.0:
            return 'GPUs with less than 3 GB of VRAM are not compatible with Stable Diffusion'
    except RuntimeError as e:
        return str(e) # Return cuda errors from mem_get_info as strings
    return None

def device_select(device):
    if device == 'cpu': return True
    if not torch.cuda.is_available(): return False
    failure_msg = device_would_fail(device)
    if failure_msg:
        if 'invalid device' in failure_msg:
            raise NameError(f'GPU "{device}" could not be found. Remove this device from config.render_devices or use one of "auto" or "cuda".')
        print(failure_msg)
        return False

    thread_data.device_name = torch.cuda.get_device_name(device)
    thread_data.device = device

    # Force full precision on 1660 and 1650 NVIDIA cards to avoid creating green images
    device_name = thread_data.device_name.lower()
    thread_data.force_full_precision = ('nvidia' in device_name or 'geforce' in device_name) and (' 1660' in device_name or ' 1650' in device_name)
    if thread_data.force_full_precision:
        print('forcing full precision on NVIDIA 16xx cards, to avoid green images. GPU detected: ', thread_data.device_name)
        # Apply force_full_precision now before models are loaded.
        thread_data.precision = 'full'

    return True

def device_init(device_selection=None):
    # Thread bound properties
    thread_data.stop_processing = False
    thread_data.temp_images = {}

    thread_data.ckpt_file = None
    thread_data.vae_file = None
    thread_data.gfpgan_file = None
    thread_data.real_esrgan_file = None

    thread_data.model = None
    thread_data.modelCS = None
    thread_data.modelFS = None
    thread_data.model_gfpgan = None
    thread_data.model_real_esrgan = None

    thread_data.model_is_half = False
    thread_data.model_fs_is_half = False
    thread_data.device = None
    thread_data.device_name = None
    thread_data.unet_bs = 1
    thread_data.precision = 'autocast'
    thread_data.sampler_plms = None
    thread_data.sampler_ddim = None

    thread_data.turbo = False
    thread_data.force_full_precision = False
    thread_data.reduced_memory = True

    if device_selection.lower() == 'cpu':
        thread_data.device = 'cpu'
        thread_data.device_name = get_processor_name()
        print('Render device CPU available as', thread_data.device_name)
        return
    if not torch.cuda.is_available():
        if device_selection == 'auto' or device_selection == 'current':
            print('WARNING: torch.cuda is not available. Using the CPU, but this will be very slow!')
            thread_data.device = 'cpu'
            thread_data.device_name = get_processor_name()
            return
        else:
            raise EnvironmentError('torch.cuda is not available.')
    device_count = torch.cuda.device_count()
    if device_count <= 1 and device_selection == 'auto':
        device_selection = 'current' # Use 'auto' only when there is more than one compatible device found.
    if device_selection == 'auto':
        print('Autoselecting GPU. Using most free memory.')
        max_mem_free = 0
        best_device = None
        for device in range(device_count):
            mem_free, mem_total = torch.cuda.mem_get_info(device)
            mem_free /= float(10**9)
            mem_total /= float(10**9)
            device_name = torch.cuda.get_device_name(device)
            print(f'GPU:{device} detected: {device_name} - Memory: {round(mem_total - mem_free, 2)}Go / {round(mem_total, 2)}Go')
            if max_mem_free < mem_free:
                max_mem_free = mem_free
                best_device = device
        if best_device and device_select(device):
            print(f'Setting GPU:{device} as active')
            torch.cuda.device(device)
            return
    if isinstance(device_selection, str):
        device_selection = device_selection.lower()
        if device_selection.startswith('gpu:'):
            device_selection = int(device_selection[4:])
    if device_selection != 'cuda' and device_selection != 'current' and device_selection != 'gpu':
        if device_select(device_selection):
            if isinstance(device_selection, int):
                print(f'Setting GPU:{device_selection} as active')
            else:
                print(f'Setting {device_selection} as active')
            torch.cuda.device(device_selection)
            return
    # By default use current device.
    print('Checking current GPU...')
    device = torch.cuda.current_device()
    device_name = torch.cuda.get_device_name(device)
    print(f'GPU:{device} detected: {device_name}')
    if device_select(device):
        return
    print('WARNING: No compatible GPU found. Using the CPU, but this will be very slow!')
    thread_data.device = 'cpu'
    thread_data.device_name = get_processor_name()

def is_first_cuda_device(device):
    if device is None: return False
    if device == 0 or device == '0': return True
    if device == 'cuda' or device == 'cuda:0': return True
    if device == 'gpu' or device == 'gpu:0': return True
    if device == 'current': return True
    if device == torch.device(0): return True
    return False

def load_model_ckpt():
    if not thread_data.ckpt_file: raise ValueError(f'Thread ckpt_file is undefined.')
    if not os.path.exists(thread_data.ckpt_file + '.ckpt'): raise FileNotFoundError(f'Cannot find {thread_data.ckpt_file}.ckpt')

    if not thread_data.precision:
        thread_data.precision = 'full' if thread_data.force_full_precision else 'autocast'

    if not thread_data.unet_bs:
        thread_data.unet_bs = 1

    if thread_data.device == 'cpu':
        thread_data.precision = 'full'

    print('loading', thread_data.ckpt_file + '.ckpt', 'to', thread_data.device, 'using precision', thread_data.precision)
    sd = load_model_from_config(thread_data.ckpt_file + '.ckpt')
    li, lo = [], []
    for key, value in sd.items():
        sp = key.split(".")
        if (sp[0]) == "model":
            if "input_blocks" in sp:
                li.append(key)
            elif "middle_block" in sp:
                li.append(key)
            elif "time_embed" in sp:
                li.append(key)
            else:
                lo.append(key)
    for key in li:
        sd["model1." + key[6:]] = sd.pop(key)
    for key in lo:
        sd["model2." + key[6:]] = sd.pop(key)

    config = OmegaConf.load(f"{config_yaml}")

    model = instantiate_from_config(config.modelUNet)
    _, _ = model.load_state_dict(sd, strict=False)
    model.eval()
    model.cdevice = torch.device(thread_data.device)
    model.unet_bs = thread_data.unet_bs
    model.turbo = thread_data.turbo
    if thread_data.device != 'cpu':
        model.to(thread_data.device)
    #if thread_data.reduced_memory:
        #model.model1.to("cpu")
        #model.model2.to("cpu")
    thread_data.model = model

    modelCS = instantiate_from_config(config.modelCondStage)
    _, _ = modelCS.load_state_dict(sd, strict=False)
    modelCS.eval()
    modelCS.cond_stage_model.device = torch.device(thread_data.device)
    if thread_data.device != 'cpu':
        if thread_data.reduced_memory:
            modelCS.to('cpu')
        else:
            modelCS.to(thread_data.device) # Preload on device if not already there.
    thread_data.modelCS = modelCS

    modelFS = instantiate_from_config(config.modelFirstStage)
    _, _ = modelFS.load_state_dict(sd, strict=False)

    if thread_data.vae_file is not None:
        if os.path.exists(thread_data.vae_file + '.vae.pt'):
            print(f"Loading VAE weights from: {thread_data.vae_file}.vae.pt")
            vae_ckpt = torch.load(thread_data.vae_file + '.vae.pt', map_location="cpu")
            vae_dict = {k: v for k, v in vae_ckpt["state_dict"].items() if k[0:4] != "loss"}
            modelFS.first_stage_model.load_state_dict(vae_dict, strict=False)
        else:
            print(f'Cannot find VAE file: {thread_data.vae_file}.vae.pt')

    modelFS.eval()
    if thread_data.device != 'cpu':
        if thread_data.reduced_memory:
            modelFS.to('cpu')
        else:
            modelFS.to(thread_data.device) # Preload on device if not already there.
    thread_data.modelFS = modelFS
    del sd

    if thread_data.device != "cpu" and thread_data.precision == "autocast":
        thread_data.model.half()
        thread_data.modelCS.half()
        thread_data.modelFS.half()
        thread_data.model_is_half = True
        thread_data.model_fs_is_half = True
    else:
        thread_data.model_is_half = False
        thread_data.model_fs_is_half = False

    print('loaded', thread_data.ckpt_file, 'as', model.device, '->', modelCS.cond_stage_model.device, '->', thread_data.modelFS.device, 'using precision', thread_data.precision)

def unload_filters():
    if thread_data.model_gfpgan is not None:
        del thread_data.model_gfpgan
    thread_data.model_gfpgan = None

    if thread_data.model_real_esrgan is not None:
        del thread_data.model_real_esrgan
    thread_data.model_real_esrgan = None

def unload_models():
    if thread_data.model is not None:
        print('Unloading models...')
        del thread_data.model
        del thread_data.modelCS
        del thread_data.modelFS

    thread_data.model = None
    thread_data.modelCS = None
    thread_data.modelFS = None

def wait_model_move_to(model, target_device): # Send to target_device and wait until complete.
    if thread_data.device == target_device: return
    start_mem = torch.cuda.memory_allocated(thread_data.device) / 1e6
    if start_mem <= 0: return
    model_name = model.__class__.__name__
    print(f'Device:{thread_data.device} - Sending model {model_name} to {target_device} | Memory transfer starting. Memory Used: {round(start_mem)}Mo')
    start_time = time.time()
    model.to(target_device)
    time_step = start_time
    WARNING_TIMEOUT = 1.5 # seconds - Show activity in console after timeout.
    last_mem = start_mem
    is_transfering = True
    while is_transfering:
        time.sleep(0.5) # 500ms
        mem = torch.cuda.memory_allocated(thread_data.device) / 1e6
        is_transfering = bool(mem > 0 and mem < last_mem) # still stuff loaded, but less than last time.
        last_mem = mem
        if not is_transfering:
            break;
        if time.time() - time_step > WARNING_TIMEOUT: # Long delay, print to console to show activity.
            print(f'Device:{thread_data.device} - Waiting for Memory transfer. Memory Used: {round(mem)}Mo, Transfered: {round(start_mem - mem)}Mo')
            time_step = time.time()
    print(f'Device:{thread_data.device} - {model_name} Moved: {round(start_mem - last_mem)}Mo in {round(time.time() - start_time, 3)} seconds to {target_device}')

def load_model_gfpgan():
    if thread_data.gfpgan_file is None: raise ValueError(f'Thread gfpgan_file is undefined.')
        #print('load_model_gfpgan called without setting gfpgan_file')
        #return
    if not is_first_cuda_device(thread_data.device):
        #TODO Remove when fixed - A bug with GFPGANer and facexlib needs to be fixed before use on other devices.
        raise Exception(f'Current device {torch.device(thread_data.device)} is not {torch.device(0)}. Cannot run GFPGANer.')
    model_path = thread_data.gfpgan_file + ".pth"
    thread_data.model_gfpgan = GFPGANer(device=torch.device(thread_data.device), model_path=model_path, upscale=1, arch='clean', channel_multiplier=2, bg_upsampler=None)
    print('loaded', thread_data.gfpgan_file, 'to', thread_data.model_gfpgan.device, 'precision', thread_data.precision)

def load_model_real_esrgan():
    if thread_data.real_esrgan_file is None: raise ValueError(f'Thread real_esrgan_file is undefined.')
        #print('load_model_real_esrgan called without setting real_esrgan_file')
        #return
    model_path = thread_data.real_esrgan_file + ".pth"

    RealESRGAN_models = {
        'RealESRGAN_x4plus': RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4),
        'RealESRGAN_x4plus_anime_6B': RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=6, num_grow_ch=32, scale=4)
    }

    model_to_use = RealESRGAN_models[thread_data.real_esrgan_file]

    if thread_data.device == 'cpu':
        thread_data.model_real_esrgan = RealESRGANer(device=torch.device(thread_data.device), scale=2, model_path=model_path, model=model_to_use, pre_pad=0, half=False) # cpu does not support half
        #thread_data.model_real_esrgan.device = torch.device(thread_data.device)
        thread_data.model_real_esrgan.model.to('cpu')
    else:
        thread_data.model_real_esrgan = RealESRGANer(device=torch.device(thread_data.device), scale=2, model_path=model_path, model=model_to_use, pre_pad=0, half=thread_data.model_is_half)

    thread_data.model_real_esrgan.model.name = thread_data.real_esrgan_file
    print('loaded ', thread_data.real_esrgan_file, 'to', thread_data.model_real_esrgan.device, 'precision', thread_data.precision)

def get_base_path(disk_path, session_id, prompt, img_id, ext, suffix=None):
    if disk_path is None: return None
    if session_id is None: return None
    if ext is None: raise Exception('Missing ext')

    session_out_path = os.path.join(disk_path, session_id)
    os.makedirs(session_out_path, exist_ok=True)

    prompt_flattened = filename_regex.sub('_', prompt)[:50]

    if suffix is not None:
        return os.path.join(session_out_path, f"{prompt_flattened}_{img_id}_{suffix}.{ext}")
    return os.path.join(session_out_path, f"{prompt_flattened}_{img_id}.{ext}")

def apply_filters(filter_name, image_data, model_path=None):
    print(f'Applying filter {filter_name}...')
    gc() # Free space before loading new data.
    if isinstance(image_data, torch.Tensor):
        print(image_data)
        image_data.to(thread_data.device)

    if filter_name == 'gfpgan':
        if model_path is not None and model_path != thread_data.gfpgan_file:
            thread_data.gfpgan_file = model_path
            load_model_gfpgan()
        elif not thread_data.model_gfpgan:
            load_model_gfpgan()
        if thread_data.model_gfpgan is None: raise Exception('Model "gfpgan" not loaded.')
        print('enhance with', thread_data.gfpgan_file, 'on', thread_data.model_gfpgan.device, 'precision', thread_data.precision)
        _, _, output = thread_data.model_gfpgan.enhance(image_data[:,:,::-1], has_aligned=False, only_center_face=False, paste_back=True)
        image_data = output[:,:,::-1]

    if filter_name == 'real_esrgan':
        if model_path is not None and model_path != thread_data.real_esrgan_file:
            thread_data.real_esrgan_file = model_path
            load_model_real_esrgan()
        elif not thread_data.model_real_esrgan:
            load_model_real_esrgan()
        if thread_data.model_real_esrgan is None: raise Exception('Model "gfpgan" not loaded.')
        print('enhance with', thread_data.real_esrgan_file, 'on', thread_data.model_real_esrgan.device, 'precision', thread_data.precision)
        output, _ = thread_data.model_real_esrgan.enhance(image_data[:,:,::-1])
        image_data = output[:,:,::-1]

    return image_data

def mk_img(req: Request):
    try:
        yield from do_mk_img(req)
    except Exception as e:
        print(traceback.format_exc())

        if thread_data.reduced_memory:
            thread_data.modelFS.to('cpu')
            thread_data.modelCS.to('cpu')
            thread_data.model.model1.to("cpu")
            thread_data.model.model2.to("cpu")
        else:
            # Model crashed, release all resources in unknown state.
            unload_models()
            unload_filters()

        gc() # Release from memory.
        yield json.dumps({
            "status": 'failed',
            "detail": str(e)
        })

def update_temp_img(req, x_samples):
    partial_images = []
    for i in range(req.num_outputs):
        x_sample_ddim = thread_data.modelFS.decode_first_stage(x_samples[i].unsqueeze(0))
        x_sample = torch.clamp((x_sample_ddim + 1.0) / 2.0, min=0.0, max=1.0)
        x_sample = 255.0 * rearrange(x_sample[0].cpu().numpy(), "c h w -> h w c")
        x_sample = x_sample.astype(np.uint8)
        img = Image.fromarray(x_sample)
        buf = BytesIO()
        img.save(buf, format='JPEG')
        buf.seek(0)

        del img, x_sample, x_sample_ddim
        # don't delete x_samples, it is used in the code that called this callback

        thread_data.temp_images[str(req.session_id) + '/' + str(i)] = buf
        partial_images.append({'path': f'/image/tmp/{req.session_id}/{i}'})
    return partial_images

# Build and return the apropriate generator for do_mk_img
def get_image_progress_generator(req, extra_props=None):
    if not req.stream_progress_updates:
        def empty_callback(x_samples, i): return x_samples
        return empty_callback

    thread_data.partial_x_samples = None
    last_callback_time = -1
    def img_callback(x_samples, i):
        nonlocal last_callback_time

        thread_data.partial_x_samples = x_samples
        step_time = time.time() - last_callback_time if last_callback_time != -1 else -1
        last_callback_time = time.time()

        progress = {"step": i, "step_time": step_time}
        if extra_props is not None:
            progress.update(extra_props)

        if req.stream_image_progress and i % 5 == 0:
            progress['output'] = update_temp_img(req, x_samples)

        yield json.dumps(progress)

        if thread_data.stop_processing:
            raise UserInitiatedStop("User requested that we stop processing")
    return img_callback

def do_mk_img(req: Request):
    thread_data.stop_processing = False

    res = Response()
    res.request = req
    res.images = []

    thread_data.temp_images.clear()

    # custom model support:
    #  the req.use_stable_diffusion_model needs to be a valid path
    #  to the ckpt file (without the extension).
    if not os.path.exists(req.use_stable_diffusion_model + '.ckpt'): raise FileNotFoundError(f'Cannot find {req.use_stable_diffusion_model}.ckpt')

    needs_model_reload = False
    if not thread_data.model or thread_data.ckpt_file != req.use_stable_diffusion_model or thread_data.vae_file != req.use_vae_model:
        thread_data.ckpt_file = req.use_stable_diffusion_model
        thread_data.vae_file = req.use_vae_model
        needs_model_reload = True

    if thread_data.device != 'cpu':
        if (thread_data.precision == 'autocast' and (req.use_full_precision or not thread_data.model_is_half)) or \
            (thread_data.precision == 'full' and not req.use_full_precision and not thread_data.force_full_precision):
            thread_data.precision = 'full' if req.use_full_precision else 'autocast'
            needs_model_reload = True

    if needs_model_reload:
        unload_models()
        unload_filters()
        load_model_ckpt()

    if thread_data.turbo != req.turbo:
        thread_data.turbo = req.turbo
        thread_data.model.turbo = req.turbo

    # Start by cleaning memory, loading and unloading things can leave memory allocated.
    gc()

    opt_prompt = req.prompt
    opt_seed = req.seed
    opt_n_iter = 1
    opt_C = 4
    opt_f = 8
    opt_ddim_eta = 0.0

    print(req, '\n    device', torch.device(thread_data.device), "as", thread_data.device_name)
    print('\n\n    Using precision:', thread_data.precision)

    seed_everything(opt_seed)

    batch_size = req.num_outputs
    prompt = opt_prompt
    assert prompt is not None
    data = [batch_size * [prompt]]

    if thread_data.precision == "autocast" and thread_data.device != "cpu":
        precision_scope = autocast
    else:
        precision_scope = nullcontext

    mask = None

    if req.init_image is None:
        handler = _txt2img

        init_latent = None
        t_enc = None
    else:
        handler = _img2img

        init_image = load_img(req.init_image, req.width, req.height)
        init_image = init_image.to(thread_data.device)

        if thread_data.device != "cpu" and thread_data.precision == "autocast":
            init_image = init_image.half()

        thread_data.modelFS.to(thread_data.device)

        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)
        init_latent = thread_data.modelFS.get_first_stage_encoding(thread_data.modelFS.encode_first_stage(init_image))  # move to latent space

        if req.mask is not None:
            mask = load_mask(req.mask, req.width, req.height, init_latent.shape[2], init_latent.shape[3], True).to(thread_data.device)
            mask = mask[0][0].unsqueeze(0).repeat(4, 1, 1).unsqueeze(0)
            mask = repeat(mask, '1 ... -> b ...', b=batch_size)

            if thread_data.device != "cpu" and thread_data.precision == "autocast":
                mask = mask.half()

        # Send to CPU and wait until complete.
        wait_model_move_to(thread_data.modelFS, 'cpu')

        assert 0. <= req.prompt_strength <= 1., 'can only work with strength in [0.0, 1.0]'
        t_enc = int(req.prompt_strength * req.num_inference_steps)
        print(f"target t_enc is {t_enc} steps")

    if req.save_to_disk_path is not None:
        session_out_path = os.path.join(req.save_to_disk_path, req.session_id)
        os.makedirs(session_out_path, exist_ok=True)
    else:
        session_out_path = None

    with torch.no_grad():
        for n in trange(opt_n_iter, desc="Sampling"):
            for prompts in tqdm(data, desc="data"):

                with precision_scope("cuda"):
                    if thread_data.reduced_memory:
                        thread_data.modelCS.to(thread_data.device)
                    uc = None
                    if req.guidance_scale != 1.0:
                        uc = thread_data.modelCS.get_learned_conditioning(batch_size * [req.negative_prompt])
                    if isinstance(prompts, tuple):
                        prompts = list(prompts)

                    subprompts, weights = split_weighted_subprompts(prompts[0])
                    if len(subprompts) > 1:
                        c = torch.zeros_like(uc)
                        totalWeight = sum(weights)
                        # normalize each "sub prompt" and add it
                        for i in range(len(subprompts)):
                            weight = weights[i]
                            # if not skip_normalize:
                            weight = weight / totalWeight
                            c = torch.add(c, thread_data.modelCS.get_learned_conditioning(subprompts[i]), alpha=weight)
                    else:
                        c = thread_data.modelCS.get_learned_conditioning(prompts)

                    if thread_data.reduced_memory:
                        thread_data.modelFS.to(thread_data.device)

                    n_steps = req.num_inference_steps if req.init_image is None else t_enc
                    img_callback = get_image_progress_generator(req, {"total_steps": n_steps})

                    # run the handler
                    try:
                        print('Running handler...')
                        if handler == _txt2img:
                            x_samples = _txt2img(req.width, req.height, req.num_outputs, req.num_inference_steps, req.guidance_scale, None, opt_C, opt_f, opt_ddim_eta, c, uc, opt_seed, img_callback, mask, req.sampler)
                        else:
                            x_samples = _img2img(init_latent, t_enc, batch_size, req.guidance_scale, c, uc, req.num_inference_steps, opt_ddim_eta, opt_seed, img_callback, mask)

                        if req.stream_progress_updates:
                            yield from x_samples
                        if hasattr(thread_data, 'partial_x_samples'):
                            if thread_data.partial_x_samples is not None:
                                x_samples = thread_data.partial_x_samples
                            del thread_data.partial_x_samples
                    except UserInitiatedStop:
                        if not hasattr(thread_data, 'partial_x_samples'):
                            continue
                        if thread_data.partial_x_samples is None:
                            del thread_data.partial_x_samples
                            continue
                        x_samples = thread_data.partial_x_samples
                        del thread_data.partial_x_samples

                    print("decoding images")
                    img_data = [None] * batch_size
                    for i in range(batch_size):
                        x_samples_ddim = thread_data.modelFS.decode_first_stage(x_samples[i].unsqueeze(0))
                        x_sample = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                        x_sample = 255.0 * rearrange(x_sample[0].cpu().numpy(), "c h w -> h w c")
                        x_sample = x_sample.astype(np.uint8)
                        img_data[i] = x_sample
                    del x_samples, x_samples_ddim, x_sample

                    if thread_data.reduced_memory:
                        # Send to CPU and wait until complete.
                        wait_model_move_to(thread_data.modelFS, 'cpu')

                    print("saving images")
                    for i in range(batch_size):
                        img = Image.fromarray(img_data[i])
                        img_id = base64.b64encode(int(time.time()+i).to_bytes(8, 'big')).decode() # Generate unique ID based on time.
                        img_id = img_id.translate({43:None, 47:None, 61:None})[-8:] # Remove + / = and keep last 8 chars.

                        has_filters =   (req.use_face_correction is not None and req.use_face_correction.startswith('GFPGAN')) or \
                                        (req.use_upscale is not None and req.use_upscale.startswith('RealESRGAN'))

                        return_orig_img = not has_filters or not req.show_only_filtered_image

                        if thread_data.stop_processing:
                            return_orig_img = True

                        if req.save_to_disk_path is not None:
                            if return_orig_img:
                                img_out_path = get_base_path(req.save_to_disk_path, req.session_id, prompts[0], img_id, req.output_format)
                                save_image(img, img_out_path)
                            meta_out_path = get_base_path(req.save_to_disk_path, req.session_id, prompts[0], img_id, 'txt')
                            save_metadata(meta_out_path, req, prompts[0], opt_seed)

                        if return_orig_img:
                            img_str = img_to_base64_str(img, req.output_format)
                            res_image_orig = ResponseImage(data=img_str, seed=opt_seed)
                            res.images.append(res_image_orig)

                            if req.save_to_disk_path is not None:
                                res_image_orig.path_abs = img_out_path
                        del img

                        if has_filters and not thread_data.stop_processing:
                            filters_applied = []
                            if req.use_face_correction:
                                img_data[i] = apply_filters('gfpgan', img_data[i], req.use_face_correction)
                                filters_applied.append(req.use_face_correction)
                            if req.use_upscale:
                                img_data[i] = apply_filters('real_esrgan', img_data[i], req.use_upscale)
                                filters_applied.append(req.use_upscale)
                            if (len(filters_applied) > 0):
                                filtered_image = Image.fromarray(img_data[i])
                                filtered_img_data = img_to_base64_str(filtered_image, req.output_format)
                                response_image = ResponseImage(data=filtered_img_data, seed=opt_seed)
                                res.images.append(response_image)
                                if req.save_to_disk_path is not None:
                                    filtered_img_out_path = get_base_path(req.save_to_disk_path, req.session_id, prompts[0], img_id, req.output_format, "_".join(filters_applied))
                                    save_image(filtered_image, filtered_img_out_path)
                                    response_image.path_abs = filtered_img_out_path
                                del filtered_image
                        # Filter Applied, move to next seed
                        opt_seed += 1

                    if thread_data.reduced_memory:
                        unload_filters()
                    del img_data
                    gc()
                    if thread_data.device != 'cpu':
                        print(f'memory_final = {round(torch.cuda.memory_allocated(thread_data.device) / 1e6, 2)}Mo')

    print('Task completed')
    yield json.dumps(res.json())

def save_image(img, img_out_path):
    try:
        img.save(img_out_path)
    except:
        print('could not save the file', traceback.format_exc())

def save_metadata(meta_out_path, req, prompt, opt_seed):
    metadata = f'''{prompt}
Width: {req.width}
Height: {req.height}
Seed: {opt_seed}
Steps: {req.num_inference_steps}
Guidance Scale: {req.guidance_scale}
Prompt Strength: {req.prompt_strength}
Use Face Correction: {req.use_face_correction}
Use Upscaling: {req.use_upscale}
Sampler: {req.sampler}
Negative Prompt: {req.negative_prompt}
Stable Diffusion model: {req.use_stable_diffusion_model + '.ckpt'}
'''
    try:
        with open(meta_out_path, 'w', encoding='utf-8') as f:
            f.write(metadata)
    except:
        print('could not save the file', traceback.format_exc())

def _txt2img(opt_W, opt_H, opt_n_samples, opt_ddim_steps, opt_scale, start_code, opt_C, opt_f, opt_ddim_eta, c, uc, opt_seed, img_callback, mask, sampler_name):
    shape = [opt_n_samples, opt_C, opt_H // opt_f, opt_W // opt_f]

    # Send to CPU and wait until complete.
    wait_model_move_to(thread_data.modelCS, 'cpu')

    if sampler_name == 'ddim':
        thread_data.model.make_schedule(ddim_num_steps=opt_ddim_steps, ddim_eta=opt_ddim_eta, verbose=False)

    samples_ddim = thread_data.model.sample(
        S=opt_ddim_steps,
        conditioning=c,
        seed=opt_seed,
        shape=shape,
        verbose=False,
        unconditional_guidance_scale=opt_scale,
        unconditional_conditioning=uc,
        eta=opt_ddim_eta,
        x_T=start_code,
        img_callback=img_callback,
        mask=mask,
        sampler = sampler_name,
    )
    yield from samples_ddim

def _img2img(init_latent, t_enc, batch_size, opt_scale, c, uc, opt_ddim_steps, opt_ddim_eta, opt_seed, img_callback, mask):
    # encode (scaled latent)
    z_enc = thread_data.model.stochastic_encode(
        init_latent,
        torch.tensor([t_enc] * batch_size).to(thread_data.device),
        opt_seed,
        opt_ddim_eta,
        opt_ddim_steps,
    )
    x_T = None if mask is None else init_latent

    # decode it
    samples_ddim = thread_data.model.sample(
        t_enc,
        c,
        z_enc,
        unconditional_guidance_scale=opt_scale,
        unconditional_conditioning=uc,
        img_callback=img_callback,
        mask=mask,
        x_T=x_T,
        sampler = 'ddim'
    )
    yield from samples_ddim

def gc():
    gc_collect()
    if thread_data.device == 'cpu':
        return
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()

# internal

def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())

def load_model_from_config(ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    return sd

# utils
class UserInitiatedStop(Exception):
    pass

def load_img(img_str, w0, h0):
    image = base64_str_to_img(img_str).convert("RGB")
    w, h = image.size
    print(f"loaded input image of size ({w}, {h}) from base64")
    if h0 is not None and w0 is not None:
        h, w = h0, w0

    w, h = map(lambda x: x - x % 64, (w, h))  # resize to integer multiple of 64
    image = image.resize((w, h), resample=Image.Resampling.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.

def load_mask(mask_str, h0, w0, newH, newW, invert=False):
    image = base64_str_to_img(mask_str).convert("RGB")
    w, h = image.size
    print(f"loaded input mask of size ({w}, {h})")

    if invert:
        print("inverted")
        image = ImageOps.invert(image)
        # where_0, where_1 = np.where(image == 0), np.where(image == 255)
        # image[where_0], image[where_1] = 255, 0

    if h0 is not None and w0 is not None:
        h, w = h0, w0

    w, h = map(lambda x: x - x % 64, (w, h))  # resize to integer multiple of 64

    print(f"New mask size ({w}, {h})")
    image = image.resize((newW, newH), resample=Image.Resampling.LANCZOS)
    image = np.array(image)

    image = image.astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return image

# https://stackoverflow.com/a/61114178
def img_to_base64_str(img, output_format="PNG"):
    buffered = BytesIO()
    img.save(buffered, format=output_format)
    buffered.seek(0)
    img_byte = buffered.getvalue()
    mime_type = "image/png" if output_format.lower() == "png" else "image/jpeg"
    img_str = f"data:{mime_type};base64," + base64.b64encode(img_byte).decode()
    return img_str

def base64_str_to_img(img_str):
    mime_type = "image/png" if img_str.startswith("data:image/png;") else "image/jpeg"
    img_str = img_str[len(f"data:{mime_type};base64,"):]
    data = base64.b64decode(img_str)
    buffered = BytesIO(data)
    img = Image.open(buffered)
    return img
