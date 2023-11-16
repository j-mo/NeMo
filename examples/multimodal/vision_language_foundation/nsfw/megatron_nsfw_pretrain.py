# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from omegaconf.omegaconf import OmegaConf, open_dict

from nemo.collections.multimodal.models.vision_language_foundation.megatron_nsfw_clip_models import (
    MegatronContentFilteringModel,
)
from nemo.collections.nlp.parts.megatron_trainer_builder import MegatronTrainerBuilder
from nemo.collections.nlp.parts.nlp_overrides import NLPSaveRestoreConnector
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="conf", config_name="megatron_nsfw_config")
def main(cfg) -> None:
    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f'\n{OmegaConf.to_yaml(cfg)}')

    assert (
        cfg.trainer.devices * cfg.trainer.num_nodes
    ) * cfg.model.micro_batch_size == cfg.model.global_batch_size, (
        "Gradient accumulation is not supported in CLIP yet."
    )

    trainer = MegatronTrainerBuilder(cfg).create_trainer()
    exp_manager(trainer, cfg.exp_manager)

    model = MegatronContentFilteringModel.restore_from(
        restore_path=cfg.model.restore_from_path,
        trainer=trainer,
        override_config_path=cfg.model,
        save_restore_connector=NLPSaveRestoreConnector(),
        strict=False,
    )

    trainer.fit(model)

    if "save_path" in cfg.model:
        logging.info(f"Saving model to path: {cfg.model.save_path}")
        model.save_to(cfg.model.save_path)


if __name__ == '__main__':
    main()