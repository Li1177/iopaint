import PIL.Image
import cv2
import torch
from diffusers import ControlNetModel
from loguru import logger
from iopaint.schema import InpaintRequest, ModelType

from .base import DiffusionInpaintModel
from .helper.controlnet_preprocess import (
    make_canny_control_image,
    make_openpose_control_image,
    make_depth_control_image,
    make_inpaint_control_image,
)
from .helper.cpu_text_encoder import CPUTextEncoderWrapper
from .utils import get_scheduler, handle_from_pretrained_exceptions


class ControlNet(DiffusionInpaintModel):
    name = "controlnet"
    pad_mod = 8
    min_size = 512

    @property
    def lcm_lora_id(self):
        if self.model_info.model_type in [
            ModelType.DIFFUSERS_SD,
            ModelType.DIFFUSERS_SD_INPAINT,
        ]:
            return "latent-consistency/lcm-lora-sdv1-5"
        if self.model_info.model_type in [
            ModelType.DIFFUSERS_SDXL,
            ModelType.DIFFUSERS_SDXL_INPAINT,
        ]:
            return "latent-consistency/lcm-lora-sdxl"
        raise NotImplementedError(f"Unsupported controlnet lcm model {self.model_info}")

    def init_model(self, device: torch.device, **kwargs):
        fp16 = not kwargs.get("no_half", False)
        model_info = kwargs["model_info"]
        controlnet_method = kwargs["controlnet_method"]

        self.model_info = model_info
        self.controlnet_method = controlnet_method

        model_kwargs = {}
        if kwargs["disable_nsfw"] or kwargs.get("cpu_offload", False):
            logger.info("Disable Stable Diffusion Model NSFW checker")
            model_kwargs.update(
                dict(
                    safety_checker=None,
                    feature_extractor=None,
                    requires_safety_checker=False,
                )
            )

        use_gpu = device == torch.device("cuda") and torch.cuda.is_available()
        torch_dtype = torch.float16 if use_gpu and fp16 else torch.float32
        self.torch_dtype = torch_dtype

        if model_info.model_type in [
            ModelType.DIFFUSERS_SD,
            ModelType.DIFFUSERS_SD_INPAINT,
        ]:
            from diffusers import (
                StableDiffusionControlNetInpaintPipeline as PipeClass,
            )
        elif model_info.model_type in [
            ModelType.DIFFUSERS_SDXL,
            ModelType.DIFFUSERS_SDXL_INPAINT,
        ]:
            from diffusers import (
                StableDiffusionXLControlNetInpaintPipeline as PipeClass,
            )

        controlnet = ControlNetModel.from_pretrained(
            pretrained_model_name_or_path=controlnet_method,
            resume_download=True,
        )
        if model_info.is_single_file_diffusers:
            if self.model_info.model_type == ModelType.DIFFUSERS_SD:
                model_kwargs["num_in_channels"] = 4
            else:
                model_kwargs["num_in_channels"] = 9

            self.model = PipeClass.from_single_file(
                model_info.path, controlnet=controlnet, **model_kwargs
            ).to(torch_dtype)
        else:
            self.model = handle_from_pretrained_exceptions(
                PipeClass.from_pretrained,
                pretrained_model_name_or_path=model_info.path,
                controlnet=controlnet,
                variant="fp16",
                dtype=torch_dtype,
                **model_kwargs,
            )

        if kwargs.get("cpu_offload", False) and use_gpu:
            logger.info("Enable sequential cpu offload")
            self.model.enable_sequential_cpu_offload(gpu_id=0)
        else:
            self.model = self.model.to(device)
            if kwargs["sd_cpu_textencoder"]:
                logger.info("Run Stable Diffusion TextEncoder on CPU")
                self.model.text_encoder = CPUTextEncoderWrapper(
                    self.model.text_encoder, torch_dtype
                )

        self.callback = kwargs.pop("callback", None)

    def switch_controlnet_method(self, new_method: str):
        self.controlnet_method = new_method
        controlnet = ControlNetModel.from_pretrained(
            new_method, torch_dtype=self.torch_dtype, resume_download=True
        ).to(self.model.device)
        self.model.controlnet = controlnet

    def _get_control_image(self, image, mask):
        if "canny" in self.controlnet_method:
            control_image = make_canny_control_image(image)
        elif "openpose" in self.controlnet_method:
            control_image = make_openpose_control_image(image)
        elif "depth" in self.controlnet_method:
            control_image = make_depth_control_image(image)
        elif "inpaint" in self.controlnet_method:
            control_image = make_inpaint_control_image(image, mask)
        else:
            raise NotImplementedError(f"{self.controlnet_method} not implemented")
        return control_image

    def forward(self, image, mask, config: InpaintRequest):
        """Input image and output image have same size
        image: [H, W, C] RGB
        mask: [H, W, 1] 255 means area to repaint
        return: BGR IMAGE
        """
        scheduler_config = self.model.scheduler.config
        scheduler = get_scheduler(config.sd_sampler, scheduler_config)
        self.model.scheduler = scheduler

        img_h, img_w = image.shape[:2]
        control_image = self._get_control_image(image, mask)
        mask_image = PIL.Image.fromarray(mask[:, :, -1], mode="L")
        image = PIL.Image.fromarray(image)

        output = self.model(
            image=image,
            mask_image=mask_image,
            control_image=control_image,
            prompt=config.prompt,
            negative_prompt=config.negative_prompt,
            num_inference_steps=config.sd_steps,
            guidance_scale=config.sd_guidance_scale,
            output_type="np",
            callback_on_step_end=self.callback,
            height=img_h,
            width=img_w,
            generator=torch.manual_seed(config.sd_seed),
            controlnet_conditioning_scale=config.controlnet_conditioning_scale,
        ).images[0]

        output = (output * 255).round().astype("uint8")
        output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)
        return output
