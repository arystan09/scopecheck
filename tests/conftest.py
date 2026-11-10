from datetime import UTC, datetime

import pytest

from scopecheck.providers.config import Generation, LiveConfig, PriceSnapshot


@pytest.fixture
def live_config():
    # Invented model/prices for offline mocks only; never an executable smoke config.
    return LiveConfig(
        model="test-model",
        generation=Generation(max_input_tokens=100_000, max_output_tokens=1000),
        price=PriceSnapshot(
            version="test-only-v1",
            model="test-model",
            input_microusd_per_million=1_000_001,
            output_microusd_per_million=2_000_001,
            context_window_tokens=100_000,
            max_output_tokens=4000,
            valid_until=datetime(2100, 1, 1, tzinfo=UTC),
            owner_verified=True,
        ),
    )
