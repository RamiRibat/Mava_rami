# python3
# Copyright 2021 InstaDeep Ltd. All rights reserved.
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

"""Tests for MADQN."""

import functools

import launchpad as lp
import sonnet as snt

import mava
from mava.components.tf.modules.exploration.exploration_scheduling import (
    LinearExplorationTimestepScheduler,
)
from mava.systems.tf import madqn
from mava.utils import lp_utils
from mava.utils.enums import ArchitectureType
from mava.utils.environments import debugging_utils


class TestMADQN:
    """Simple integration/smoke test for MADQN."""

    def test_madqn_on_debugging_env(self) -> None:
        """Test feedforward madqn."""
        # environment
        environment_factory = functools.partial(
            debugging_utils.make_environment,
            env_name="simple_spread",
            action_space="discrete",
        )

        # networks
        network_factory = lp_utils.partial_kwargs(
            madqn.make_default_networks, value_networks_layer_sizes=(64, 64)
        )

        # system
        system = madqn.MADQN(
            environment_factory=environment_factory,
            network_factory=network_factory,
            num_executors=1,
            batch_size=32,
            min_replay_size=32,
            max_replay_size=1000,
            optimizer=snt.optimizers.Adam(learning_rate=1e-3),
            checkpoint=False,
            exploration_scheduler_fn=LinearExplorationTimestepScheduler(
                epsilon_start=1.0, epsilon_min=0.05, epsilon_decay_steps=500
            ),
        )

        program = system.build()

        (trainer_node,) = program.groups["trainer"]
        trainer_node.disable_run()

        # Launch gpu config - don't use gpu
        local_resources = lp_utils.to_device(
            program_nodes=program.groups.keys(), nodes_on_gpu=[]
        )
        lp.launch(
            program,
            launch_type="test_mt",
            local_resources=local_resources,
        )

        trainer: mava.Trainer = trainer_node.create_handle().dereference()

        for _ in range(2):
            trainer.step()

    def test_recurrent_madqn_on_debugging_env(self) -> None:
        """Test recurrent madqn."""

        # environment
        environment_factory = functools.partial(
            debugging_utils.make_environment,
            env_name="simple_spread",
            action_space="discrete",
        )

        # networks
        network_factory = lp_utils.partial_kwargs(
            madqn.make_default_networks,
            architecture_typee=ArchitectureType.recurrent,
            value_networks_layer_sizes=(32, 32),
        )

        # system
        system = madqn.MADQN(
            environment_factory=environment_factory,
            network_factory=network_factory,
            num_executors=1,
            batch_size=16,
            min_replay_size=16,
            max_replay_size=1000,
            optimizer=snt.optimizers.Adam(learning_rate=1e-3),
            checkpoint=False,
            trainer_fn=madqn.training.MADQNRecurrentTrainer,
            executor_fn=madqn.execution.MADQNRecurrentExecutor,
            sequence_length=4,
            period=4,
            exploration_scheduler_fn=LinearExplorationTimestepScheduler(
                epsilon_start=1.0, epsilon_min=0.05, epsilon_decay_steps=500
            ),
        )

        program = system.build()

        (trainer_node,) = program.groups["trainer"]
        trainer_node.disable_run()

        # Launch gpu config - don't use gpu
        local_resources = lp_utils.to_device(
            program_nodes=program.groups.keys(), nodes_on_gpu=[]
        )
        lp.launch(
            program,
            launch_type="test_mt",
            local_resources=local_resources,
        )

        trainer: mava.Trainer = trainer_node.create_handle().dereference()

        for _ in range(2):
            trainer.step()
