import os

import pytest
import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer

from nemo.collections.multimodal.models.generative.controlnet.controlnet import MegatronControlNet
from nemo.collections.multimodal.data.controlnet.controlnet_dataset import build_train_valid_datasets
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy

DEVICE_CAPABILITY = None
if torch.cuda.is_available():
    DEVICE_CAPABILITY = torch.cuda.get_device_capability()


@pytest.fixture()
def model_cfg():
    model_cfg_string = """
        precision: ${trainer.precision}
        # specify micro_batch_size, global_batch_size, and model parallelism
        # gradient accumulation will be done automatically based on data_parallel_size
        micro_batch_size: 4 # limited by GPU memory
        global_batch_size: 8

        linear_start: 0.00085
        linear_end: 0.0120
        num_timesteps_cond: 1
        log_every_t: 200
        timesteps: 1000
        first_stage_key: images
        cond_stage_key: captions
        control_key: hint
        image_size: 64
        channels: 4
        cond_stage_trainable: false
        conditioning_key: crossattn
        monitor: val/loss_simple_ema
        scale_factor: 0.18215
        use_ema: False
        scale_by_std: False
        ckpt_path:
        ignore_keys: [ ]
        parameterization: eps
        clip_denoised: True
        load_only_unet: False
        cosine_s: 8e-3
        given_betas:
        original_elbo_weight: 0
        v_posterior: 0
        l_simple_weight: 1
        use_positional_encodings: False
        learn_logvar: False
        logvar_init: 0
        beta_schedule: linear
        loss_type: l2
        learning_rate: 1.0e-04
        concat_mode: True
        cond_stage_forward:
        text_embedding_dropout_rate: 0.0
        fused_opt: True
        inductor: False
        inductor_cudagraphs: False
        capture_cudagraph_iters: -1 # -1 to disable
        channels_last: True
        only_mid_control: False
        sd_locked: True

        control_stage_config:
          _target_: nemo.collections.multimodal.models.controlnet.controlnet.ControlNet
          params:
            from_pretrained_unet: /ckpts/v1-5-pruned.ckpt
            from_NeMo: True
            image_size: 32 # unused
            in_channels: 4
            hint_channels: 3
            model_channels: 320
            attention_resolutions: [ 4, 2, 1 ]
            num_res_blocks: 2
            channel_mult: [ 1, 2, 4, 4 ]
            num_heads: 8
            use_spatial_transformer: True
            use_linear_in_transformer: False
            transformer_depth: 1
            context_dim: 768
            use_checkpoint: False
            legacy: False
            use_flash_attention: False

        unet_config:
          _target_: nemo.collections.multimodal.models.controlnet.controlnet.ControlledUnetModel
          from_pretrained:
          from_NeMo: True
          image_size: 32 # unused
          in_channels: 4
          out_channels: 4
          model_channels: 320
          attention_resolutions:
          - 4
          - 2
          - 1
          num_res_blocks: 2
          channel_mult:
          - 1
          - 2
          - 4
          - 4
          num_heads: 8
          use_spatial_transformer: True
          transformer_depth: 1
          context_dim: 768
          use_checkpoint: False
          legacy: False
          use_flash_attention: False

        first_stage_config:
          _target_: nemo.collections.multimodal.models.stable_diffusion.ldm.autoencoder.AutoencoderKL
          from_pretrained:
          embed_dim: 4
          monitor: val/rec_loss
          ddconfig:
            double_z: true
            z_channels: 4
            resolution: 256
            in_channels: 3
            out_ch: 3
            ch: 128
            ch_mult:
            - 1
            - 2
            - 4
            - 4
            num_res_blocks: 2
            attn_resolutions: []
            dropout: 0.0
          lossconfig:
            target: torch.nn.Identity

        cond_stage_config:
          _target_: nemo.collections.multimodal.modules.stable_diffusion.encoders.modules.FrozenCLIPEmbedder
          version: openai/clip-vit-large-patch14
          device: cuda
          max_length: 77

        data:
          num_workers: 16
          synthetic_data: True # dataset_path and local_root_path can be empty when using synthetic data
          synthetic_data_length: 10000
          train:
            dataset_path:
              #- /datasets/tarfiles/fill50k.pkl
              - /datasets/coco-stuff/coco-stuff-tarfiles/wdinfo-coco-stuff.pkl
            augmentations:
              resize_smallest_side: 512
              center_crop_h_w: 512, 512
              horizontal_flip: False
          webdataset:
            infinite_sampler: False
            local_root_path: /datasets/coco-stuff/coco-stuff-tarfiles

        optim:
          name: fused_adam
          lr: 2e-5
          weight_decay: 0.
          betas:
            - 0.9
            - 0.999
          sched:
            name: WarmupHoldPolicy
            warmup_steps: 0
            hold_steps: 10000000000000 # Incredibly large value to hold the lr as constant

          # Nsys profiling options
        nsys_profile:
          enabled: False
          start_step: 10  # Global batch to start profiling
          end_step: 10 # Global batch to end profiling
          ranks: [ 0 ] # Global rank IDs to profile
          gen_shape: False # Generate model and kernel details including input shapes

        image_logger:
          batch_frequency: 1000
          max_images: 0

        #miscellaneous
        seed: 1234
        resume_from_checkpoint: null # manually set the checkpoint file to load from
        apex_transformer_log_level: 30 # Python logging level displays logs with severity greater than or equal to this
        gradient_as_bucket_view: True # PyTorch DDP argument. Allocate gradients in a contiguous bucket to save memory (less fragmentation and buffer memory)
    """
    model_cfg = OmegaConf.create(model_cfg_string)
    return model_cfg


@pytest.fixture()
def trainer_cfg():
    trainer_cfg_string = """
      devices: 2
      num_nodes: 1
      accelerator: gpu
      precision: 16
      logger: False # logger provided by exp_manager
      enable_checkpointing: False
      use_distributed_sampler: True
      max_epochs: 3 # PTL default. In practice, max_steps will be reached first.
      max_steps: -1 # consumed_samples = global_step * micro_batch_size * data_parallel_size * accumulate_grad_batches
      log_every_n_steps: 10
      accumulate_grad_batches: 1 # do not modify, grad acc is automatic for training megatron models
      gradient_clip_val: 1.0
      benchmark: False
      enable_model_summary: True
      limit_val_batches: 0
    """
    trainer_cfg = OmegaConf.create(trainer_cfg_string)

    return trainer_cfg


@pytest.fixture()
def exp_manager_cfg():
    exp_manager_cfg_string = """
      explicit_log_dir: null
      exp_dir: null
      name: controlnet
      create_wandb_logger: False
      wandb_logger_kwargs:
        project: stable-diffusion
        group: controlnet
        name: controlnet-v1.5
        resume: True
      create_checkpoint_callback: True
      create_tensorboard_logger: True
      checkpoint_callback_params:
        save_top_k: -1
        every_n_train_steps: 5000
        every_n_epochs: 0
        monitor: reduced_train_loss
        filename: 'controlnet--{reduced_train_loss:.2f}-{step}-{consumed_samples}'
      resume_if_exists: True
      resume_ignore_no_checkpoint: True
      resume_from_checkpoint: ${model.resume_from_checkpoint}
      ema:
        enable: False
        decay: 0.9999
        validate_original_weights: False
        every_n_steps: 1
        cpu_offload: False
    """

    exp_manager_cfg = OmegaConf.create(exp_manager_cfg_string)

    return exp_manager_cfg


@pytest.fixture()
def precision():
    return 32


@pytest.fixture()
def controlnet_trainer_and_model(model_cfg, trainer_cfg, precision):
    model_cfg['precision'] = precision
    trainer_cfg['precision'] = precision

    strategy = NLPDDPStrategy()

    trainer = Trainer(strategy=strategy, **trainer_cfg)

    cfg = DictConfig(model_cfg)

    model = MegatronControlNet(cfg=cfg, trainer=trainer)

    def dummy():
        return

    if model.trainer.strategy.launcher is not None:
        model.trainer.strategy.launcher.launch(dummy, trainer=model.trainer)

    model.trainer.strategy.setup_environment()

    return trainer, model


@pytest.mark.run_only_on('GPU')
class TestMegatronControlNet:
    @pytest.mark.unit
    def test_constructor(self, controlnet_trainer_and_model):
        controlnet_model = controlnet_trainer_and_model[1]
        assert isinstance(controlnet_model, MegatronControlNet)

        num_weights = controlnet_model.num_weights
        assert num_weights == 361279120

    @pytest.mark.unit
    def test_build_dataset(self, controlnet_trainer_and_model):
        controlnet_model = controlnet_trainer_and_model[1]
        train_ds, valid_ds = build_train_valid_datasets(model_cfg=controlnet_model.cfg, consumed_samples=0)

        assert len(train_ds) == controlnet_model.cfg.data.synthetic_data_length
        sample = next(iter(train_ds))
        assert "images" in sample
        assert "captions" in sample
        assert "hint" in sample

    @pytest.mark.parametrize(
        "precision",
        [
            32,
            16,
            pytest.param(
                "bf16",
                marks=pytest.mark.skipif(
                    not DEVICE_CAPABILITY or DEVICE_CAPABILITY[0] < 8,
                    reason='bfloat16 is not supported on this device',
                ),
            ),
        ],
    )
    def test_forward(self, controlnet_trainer_and_model, test_data_dir, precision=None):
        trainer, controlnet_model = controlnet_trainer_and_model

        dtype = None
        if controlnet_model.cfg['precision'] in [32, '32', '32-true']:
            dtype = torch.float
        elif controlnet_model.cfg['precision'] in [16, '16', '16-mixed']:
            dtype = torch.float16
        elif controlnet_model.cfg['precision'] in ['bf16', 'bf16-mixed']:
            dtype = torch.bfloat16
        else:
            raise ValueError(f"precision: {controlnet_model.cfg['precision']} is not supported.")

        controlnet_model = controlnet_model.cuda()
        controlnet_model.eval()

        train_ds, _ = build_train_valid_datasets(controlnet_model.cfg, 0)
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=controlnet_model.cfg.micro_batch_size)
        batch = next(iter(train_loader))
        batch[controlnet_model.cfg.first_stage_key] = batch[controlnet_model.cfg.first_stage_key].cuda(
            non_blocking=True
        )
        x, c = controlnet_model.model.get_input(batch, controlnet_model.cfg.first_stage_key)

        if not isinstance(c, dict):
            batch = [x, c]

        elif len(controlnet_model.conditioning_keys) == 0:
            controlnet_model.conditioning_keys = list(c.keys())
            c_list = [c[key] for key in controlnet_model.conditioning_keys]
            batch = [x, *c_list]
        batch = [x.cuda(non_blocking=True) for x in batch]
        if len(controlnet_model.conditioning_keys) == 0:
            x, c = batch
        else:
            x = batch[0]
            c = {}
            for idx, key in enumerate(controlnet_model.conditioning_keys):
                c[key] = batch[1 + idx]
        with torch.no_grad():
            loss, _ = controlnet_model.model(x, c)
        assert loss.dtype == torch.float
