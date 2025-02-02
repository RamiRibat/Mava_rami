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

"""Parameter client for Jax system. Adapted from Deepmind's Acme library"""

from concurrent import futures
from typing import Any, Dict, List, Optional, Tuple, Union

import jax
import numpy as np

from mava.systems.parameter_server import ParameterServer
from mava.utils.done_future import DoneFuture
from mava.utils.sort_utils import sort_str_num


class ParameterClient:
    """A parameter client for updating parameters from a remote server."""

    def __init__(
        self,
        server: ParameterServer,
        parameters: Dict[str, Any],
        multi_process: bool,
        get_keys: Optional[List[str]] = None,
        set_keys: Optional[List[str]] = None,
        update_period: int = 1,
        devices: Dict[str, Optional[Union[str, jax.xla.Device]]] = {},
    ):
        """Initialise the parameter client.

        Args:
            server: the system parameter server.
            parameters: parameters that the client tracks.
            multi_process: wheter or not to make async calls to the server, server must
                be a launchpad node for this to work.
            get_keys: names of parameters to fetch from the server in requests.
            set_keys: names of parameters to set in the server.
            update_period: number of calls between syncs with the server.
            devices: dictionary {parameter name: device} defining devices for params.
        """
        self._all_keys = sort_str_num(list(parameters.keys()))
        # TODO (dries): Is the below change correct?
        self._get_keys = get_keys if get_keys is not None else []
        self._set_keys = set_keys if set_keys is not None else []
        self._parameters: Dict[str, Any] = parameters
        self._multi_process = multi_process
        self._get_call_counter = 0
        self._set_call_counter = 0
        self._set_get_call_counter = 0
        self._update_period = update_period
        self._server = server
        self._devices = devices

        # note below it is assumed that if one device is specified with a string
        # they all are - need to test this works
        # TODO: (Dries/Arnu): check this
        if len(self._devices) and isinstance(list(self._devices.values())[0], str):
            for key, device in self._devices.items():
                self._devices[key] = jax.devices(device)[0]  # type: ignore

        self._request = lambda: server.get_parameters(self._get_keys)
        self._request_all = lambda: server.get_parameters(self._all_keys)

        self._adjust = lambda: server.set_parameters(
            {key: self._parameters[key] for key in self._set_keys},
        )
        self._adjust_param = lambda params: server.set_parameters(params)

        self._add = lambda params: server.add_to_parameters(params)
        self._async_add_buffer: Dict[str, Any] = {}

        # parameter server only has `futures` attribute if it is a launchpad node
        # and it is only a launchpad node if we are running in multiprocess
        if multi_process:
            self._async_request = lambda: server.futures.get_parameters(self._get_keys)  # type: ignore # noqa
            self._async_adjust = lambda: server.futures.set_parameters(  # type: ignore
                {key: self._parameters[key] for key in self._set_keys},
            )
            self._async_adjust_param = lambda params: server.futures.set_parameters(params)  # type: ignore # noqa
            self._async_add = lambda params: server.futures.add_to_parameters(params)  # type: ignore # noqa
        else:
            self._async_request = lambda: DoneFuture(self._request())
            self._async_adjust = lambda: DoneFuture(self._adjust())
            self._async_adjust_param = lambda params: DoneFuture(
                self._adjust_param(params)
            )
            self._async_add = lambda params: DoneFuture(self._add(params))

        # Initialize this client's future to None to indicate to the `update()`
        # method that there is no pending/running request.
        self._get_future: Optional[futures.Future] = None
        self._set_future: Optional[futures.Future] = None
        self._set_get_future: Optional[Tuple[futures.Future, futures.Future]] = None
        self._add_future: Optional[futures.Future] = None

    def _adjust_and_request(self) -> None:
        """Set the parameters in the server, then update local params from the server.

        Returns:
            None.
        """
        self._server.set_parameters(
            {key: self._parameters[key] for key in self._set_keys},
        )
        self._copy(self._server.get_parameters(self._get_keys))

    def _async_adjust_and_request(
        self,
    ) -> Optional[Tuple[futures.Future, futures.Future]]:
        """Set the parameters in the server, then update local params from the server.

        Returns:
            A future for the parameter get
        """
        # Do not do parallel calls if not multiprocess
        if not self._multi_process:
            self._adjust_and_request()
            return None

        # parameter server only has `futures` attribute if it is a launchpad node
        # and it is only a launchpad node if we are running in multiprocess
        set_future = self._server.futures.set_parameters(  # type: ignore
            {key: self._parameters[key] for key in self._set_keys},
        )
        # Get all parameters in _get_keys that we didn't set above with _set_keys
        get_keys = set(self._get_keys) - set(self._set_keys)
        get_future = self._server.futures.get_parameters(get_keys)  # type: ignore
        get_future.add_done_callback(lambda ctx: self._copy(ctx.result()))

        return set_future, get_future

    def get_async(self) -> None:
        """Asynchronously updates the parameters with the latest copy from server.

        Returns:
            None.
        """
        # Track the number of calls (we only update periodically).
        if self._get_call_counter < self._update_period:
            self._get_call_counter += 1

        period_reached: bool = self._get_call_counter >= self._update_period
        if period_reached and self._get_future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            self._get_future = self._async_request()
            self._get_call_counter = 0

        if self._get_future is not None and self._get_future.done():
            # The active request is done so copy the result and remove the future.\
            self._copy(self._get_future.result())
            self._get_future = None

    def set_async(self, params: Optional[Dict[str, Any]] = None) -> None:
        """Asynchronously updates server with the set parameters.

        Returns:
            None.
        """
        # Track the number of calls (we only update periodically).
        if self._set_call_counter < self._update_period:
            self._set_call_counter += 1

        period_reached: bool = self._set_call_counter >= self._update_period

        if period_reached and self._set_future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            if params is None:
                self._set_future = self._async_adjust()
            else:
                self._set_future = self._async_adjust_param(params)
            self._set_call_counter = 0
            return
        if self._set_future is not None and self._set_future.done():
            self._set_future = None

    def set_and_get_async(self) -> None:
        """Asynchronously updates server and gets from server.

        Returns:
            None.
        """
        # Track the number of calls (we only update periodically).
        if self._set_get_call_counter < self._update_period:
            self._set_get_call_counter += 1
        period_reached: bool = self._set_get_call_counter >= self._update_period

        if period_reached and self._set_get_future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            self._set_get_future = self._async_adjust_and_request()
            self._set_get_call_counter = 0
            return

        if self._set_get_future is not None and all(
            [f.done() for f in self._set_get_future]
        ):
            self._set_get_future = None

    def add_async(self, params: Dict[str, Any]) -> None:
        """Asynchronously adds to server parameters.

        Returns:
            None.
        """
        if self._add_future is not None and self._add_future.done():
            self._add_future = None

        names = params.keys()
        if self._add_future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            if not self._async_add_buffer:
                self._add_future = self._async_add(params)
            else:
                for name in names:
                    self._async_add_buffer[name] += params[name]

                self._add_future = self._async_add(self._async_add_buffer)
                self._async_add_buffer = {}
            return
        else:
            # The trainers is going to fast to keep up! Adding
            # all the values up and only writing them when the
            # process is ready.
            if self._async_add_buffer:
                for name in names:
                    self._async_add_buffer[name] += params[name]
            else:
                for name in names:
                    self._async_add_buffer[name] = params[name]

    def add_and_wait(self, params: Dict[str, Any]) -> None:
        """Add to the given parameters in the server. Wait for completion.

        Adds the specified parameters to the corresponding parameters in server
        and waits for the process to complete before continuing.

        Args:
            params: dictionary {param name: value to add to param}.

        Returns:
            None.
        """
        self._server.add_to_parameters(params)

    def get_and_wait(self) -> None:
        """Update get parameters from server. Wait for completion.

        Updates the get parameters with the latest copy from server
        and waits for the process to complete before continuing.

        Returns:
            None.
        """
        self._copy(self._request())

    def get_all_and_wait(self) -> None:
        """Update all parameters from server. Wait for completion.

        Updates all the parameters with the latest copy from server
        and waits for the process to complete before continuing.

        Returns:
            None.
        """
        self._copy(self._request_all())

    def set_and_wait(self, params: Optional[Dict[str, Any]] = None) -> None:
        """Update server with set parameters. Wait for completion.

        Updates server with the set parameters
        and waits for the process to complete before continuing.

        Returns:
            None.
        """
        if params is None:
            self._adjust()
        else:
            self._adjust_param(params)

    # TODO(Dries/Arnu): this needs a bit of a cleanup
    def _copy(self, new_parameters: Dict[str, Any]) -> None:
        """Copy the given new parameters to the existing ones.

        Args:
            new_parameters: dictionary {parameter name: new parameter value}.

        Returns:
            None.
        """
        for key in new_parameters.keys():
            if isinstance(new_parameters[key], dict):
                for type1_key in new_parameters[key].keys():
                    # Check if nested dictionary
                    if isinstance(new_parameters[key][type1_key], dict):
                        for type2_key in self._parameters[key][type1_key].keys():
                            if self._devices:
                                # Move variables to a proper device.
                                # self._parameters[key][type1_key][
                                #     type2_key
                                # ] = jax.device_put(  # type: ignore
                                #     new_parameters[key][type1_key],
                                #     self._devices[key][type1_key],
                                # )
                                raise NotImplementedError(
                                    "Support for devices"
                                    + "have not been implemented"
                                    + "yet in the parameter client."
                                )
                            else:
                                self._parameters[key][type1_key][
                                    type2_key
                                ] = new_parameters[key][type1_key][type2_key]
                    else:
                        self._parameters[key][type1_key] = new_parameters[key][
                            type1_key
                        ]
            elif isinstance(new_parameters[key], np.ndarray):
                if self._devices:
                    self._parameters[key] = jax.device_put(
                        new_parameters[key], self._devices[key]  # type: ignore
                    )
                else:
                    # Note (dries): These in-place operators are used instead
                    # of direct assignment to not lose reference to the numpy
                    # array.

                    self._parameters[key] *= 0
                    # Remove last dim of numpy array if needed
                    if new_parameters[key].shape != self._parameters[key].shape:
                        self._parameters[key] += new_parameters[key][0]
                    else:
                        self._parameters[key] += new_parameters[key]
            elif isinstance(new_parameters[key], tuple):
                for i in range(len(self._parameters[key])):
                    if self._devices:
                        self._parameters[key][i] = jax.device_put(
                            new_parameters[key][i],
                            self._devices[key][i],  # type: ignore
                        )
                    else:
                        self._parameters[key][i] = new_parameters[key][i]
            else:
                raise NotImplementedError(
                    f"""Parameter type {type(new_parameters[key])} of '{key}' not implemented.
                    Please use a mutable type for '{key}'"""
                )
