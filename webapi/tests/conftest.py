# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND.
# See the License for the specific language governing permissions
# and limitations under the License.

import pytest


@pytest.fixture(autouse=True)
def isolated_metadata_catalog(monkeypatch, tmp_path):
    """Every app a test starts keeps its catalog in memory and crawls nothing.

    Without this the lifespan would open the real business database's
    ``lattice_metadata`` schema and the scheduler would register the test's
    temporary data sources there.
    """
    monkeypatch.setenv("LATTICE_METADATA_DSN", "sqlite:///:memory:")
    monkeypatch.setenv("LATTICE_METADATA_SCHEDULER", "0")
    # Authentication stays off unless a test turns it on, and its accounts never
    # land in the developer's own .runtime database.
    monkeypatch.setenv("LATTICE_AUTH", "0")
    monkeypatch.setenv("LATTICE_AUTH_DB", str(tmp_path / "auth.sqlite"))
