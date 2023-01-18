# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""
Given NMT model's .nemo file(s), this script can be used to translate text.
USAGE Example:
1. Obtain text file in src language. You can use sacrebleu to obtain standard test sets like so:
    sacrebleu -t wmt14 -l de-en --echo src > wmt14-de-en.src
2. Translate:
    python nmt_transformer_infer.py --model=[Path to .nemo file(s)] --srctext=wmt14-de-en.src --tgtout=wmt14-de-en.pre
"""


import os
import numpy as np

from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.machine_translation.megatron_nmt_model import MegatronNMTModel
from nemo.collections.nlp.models.machine_translation.megatron_nmt_retrieval_model import MegatronNMTRetrievalModel
from nemo.collections.nlp.modules.common.megatron.megatron_init import fake_initialize_model_parallel
from nemo.collections.nlp.modules.common.megatron.utils import ApexGuardDefaults
from nemo.collections.nlp.parts.nlp_overrides import NLPDDPStrategy, NLPSaveRestoreConnector
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.app_state import AppState
from nemo.utils.model_utils import inject_model_parallel_rank

try:
    from apex.transformer.pipeline_parallel.utils import _reconfigure_microbatch_calculator

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    ModelType = ApexGuardDefaults()
    HAVE_APEX = False


@hydra_runner(config_path="conf", config_name="nmt_retrieval_megatron_infer")
def main(cfg) -> None:

    # trainer required for restoring model parallel models
    trainer = Trainer(strategy=NLPDDPStrategy(), **cfg.trainer)
    assert (
        cfg.trainer.devices * cfg.trainer.num_nodes
        == cfg.tensor_model_parallel_size * cfg.pipeline_model_parallel_size
    ), "devices * num_nodes should equal tensor_model_parallel_size * pipeline_model_parallel_size"

    app_state = AppState()
    app_state.model_parallel_size = cfg.tensor_model_parallel_size * cfg.pipeline_model_parallel_size
    (
        app_state.tensor_model_parallel_rank,
        app_state.pipeline_model_parallel_rank,
        app_state.model_parallel_size,
        app_state.data_parallel_size,
        app_state.pipeline_model_parallel_split_rank,
        app_state.virtual_pipeline_model_parallel_rank,
    ) = fake_initialize_model_parallel(
        world_size=app_state.model_parallel_size,
        rank=trainer.global_rank,
        tensor_model_parallel_size_=cfg.tensor_model_parallel_size,
        pipeline_model_parallel_size_=cfg.pipeline_model_parallel_size,
        pipeline_model_parallel_split_rank_=cfg.pipeline_model_parallel_split_rank,
    )

    class_name = 'MegatronNMTRetrievalModel' if cfg.retrieval else 'MegatronNMTModel'

    if cfg.model_file is not None:
        if not os.path.exists(cfg.model_file):
            raise ValueError(f"Model file {cfg.model_file} does not exist")
        model = eval(class_name).restore_from(
            restore_path=cfg.model_file, trainer=trainer, save_restore_connector=NLPSaveRestoreConnector(),
        )
    elif cfg.checkpoint_dir is not None:
        checkpoint_path = inject_model_parallel_rank(os.path.join(cfg.checkpoint_dir, cfg.checkpoint_name))
        model = eval(class_name).load_from_checkpoint(checkpoint_path, hparams_file=cfg.hparams_file, trainer=trainer)
    else:
        raise ValueError("need at least a nemo file or checkpoint dir")

    model.freeze()

    logging.info(f"Translating: {cfg.srctext}")
    src_text = []
    
    # Setup retrieval dataset
    if cfg.retrieval:
        model.setup_retrieval_dataset(cfg)
        nn_list = np.load(cfg.nn_mapping)[:, : cfg.num_neighbors]
        neighbors = []
    translations = []
    with open(cfg.srctext, 'r') as src_f, open(cfg.tgtout, 'w') as tgt_f, open(cfg.tgtout + '.neighs', 'w') as src_out_f:
        print('loaded')
        for i, line in enumerate(src_f):
            src_text.append(line.strip())
            if cfg.retrieval:
                neighbors.append(nn_list[i].tolist())
            if len(src_text) == cfg.batch_size:
                if cfg.retrieval:
                    translations, ret_enc_add = model.translate(
                        text=src_text, source_lang=cfg.source_lang, target_lang=cfg.target_lang, neighbors=neighbors,
                    )
                else:
                    translations = model.translate(
                        text=src_text, source_lang=cfg.source_lang, target_lang=cfg.target_lang,
                    )
                for inp, added, translation in zip(src_text, ret_enc_add, translations):
                    # print("SOURCE", inp + added + "\n")
                    src_out_f.write(inp + added + "\n")
                    # print("TARGET", translation + "\n")
                    tgt_f.write(translation + "\n")

                src_text = []
        if len(src_text) > 0:
            if cfg.retrieval:
                translations, ret_enc_add = model.translate(
                    text=src_text, source_lang=cfg.source_lang, target_lang=cfg.target_lang, neighbors=neighbors,
                )
            else:
                translations = model.translate(text=src_text, source_lang=cfg.source_lang, target_lang=cfg.target_lang,)
            for inp, added in zip(src_text, ret_enc_add):
                src_out_f.write(inp + added + "\n")
            for translation in translations:
                tgt_f.write(translation + "\n")


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter