"""Fixtures for the Openhome integration tests."""

import base64
from collections.abc import Generator
import struct
from unittest.mock import AsyncMock, MagicMock, patch

from openhomedevice.device import Device
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.openhome.const import DOMAIN
from homeassistant.const import CONF_HOST, Platform

HOST = "http://localhost"

# Playlist service action results: track id 2 is current, ids 1-2-3 are in the
# playlist and a new Insert gets id 4.
PLAYLIST_ACTION_RESULTS = {
    "Id": {"Value": 2},
    "IdArray": {
        "Array": base64.b64encode(struct.pack(">3I", 1, 2, 3)).decode("ascii"),
        "Token": 1,
    },
    "Insert": {"NewId": 4},
}

TRACK_INFO = {
    "albumArtwork": "http://localhost/album.jpg",
    "albumTitle": "album_title",
    "artist": ["artist"],
    "title": "title",
    "uri": "http://localhost/track.flac",
}

SOURCES = [
    {"index": 0, "name": "Playlist", "type": "Playlist"},
    {"index": 1, "name": "Radio", "type": "Radio"},
]

# The device coroutines the actions drive, referenced from the library so a
# rename upstream fails the tests rather than leaving an action uncovered.
ACTION_METHODS = (
    Device.set_standby,
    Device.play,
    Device.pause,
    Device.stop,
    Device.skip,
    Device.increase_volume,
    Device.decrease_volume,
    Device.set_volume,
    Device.set_mute,
    Device.set_source,
    Device.play_media,
    Device.invoke_pin,
)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mocked config entry."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST}, unique_id="uuid")


@pytest.fixture
def platforms() -> list[Platform]:
    """Platforms to load; override per module or per test."""
    return []


@pytest.fixture(autouse=True)
def patch_platforms(platforms: list[Platform]) -> Generator[None]:
    """Load only the platforms the test asks for."""
    with patch("homeassistant.components.openhome.PLATFORMS", platforms):
        yield


@pytest.fixture
def mock_device_class() -> Generator[MagicMock]:
    """Return the patched Openhome Device class."""
    with patch("homeassistant.components.openhome.Device", MagicMock()) as mock_class:
        device = mock_class.return_value
        device.init = AsyncMock()
        device.uuid = MagicMock(return_value="uuid")
        device.manufacturer = MagicMock(return_value="manufacturer")
        device.model_name = MagicMock(return_value="model_name")
        device.friendly_name = MagicMock(return_value="friendly_name")
        device.volume_enabled = True
        device.pins_enabled = True
        device.room = AsyncMock(return_value="room")
        device.track_info = AsyncMock(return_value={})
        device.volume = AsyncMock(return_value=50)
        device.is_muted = AsyncMock(return_value=False)
        device.sources = AsyncMock(return_value=SOURCES)
        device.source = AsyncMock(return_value=SOURCES[0])
        device.is_in_standby = AsyncMock(return_value=False)
        device.transport_state = AsyncMock(return_value="Playing")
        device.software_status = AsyncMock(return_value=None)
        device.update_firmware = AsyncMock()
        for method in ACTION_METHODS:
            setattr(device, method.__name__, AsyncMock())
        # The Playlist service actions the enqueue modes drive, one mock per
        # action name, kept on the device so tests can assert on each call.
        playlist_actions: dict[str, MagicMock] = {}

        def playlist_action(name: str) -> MagicMock:
            if name not in playlist_actions:
                action = MagicMock()
                action.async_call = AsyncMock(
                    return_value=PLAYLIST_ACTION_RESULTS.get(name, {})
                )
                playlist_actions[name] = action
            return playlist_actions[name]

        device.playlist_service = MagicMock()
        device.playlist_service.action = MagicMock(side_effect=playlist_action)
        device.playlist_actions = playlist_actions
        yield mock_class


@pytest.fixture
def mock_device(mock_device_class: MagicMock) -> MagicMock:
    """Return a mocked Openhome device that polls successfully."""
    return mock_device_class.return_value
