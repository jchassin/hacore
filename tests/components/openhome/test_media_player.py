"""Tests for the Openhome media player platform."""

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import MagicMock, call, patch

from async_upnp_client.exceptions import UpnpError
from openhomedevice import didl_lite
from openhomedevice.device import Device
from openhomedevice.exceptions import OpenhomeConnectionError
import pytest
from pytest_homeassistant.common import MockConfigEntry, snapshot_platform
from syrupy.assertion import SnapshotAssertion

from homeassistant.components.media_player import (
    ATTR_INPUT_SOURCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_ENQUEUE,
    ATTR_MEDIA_VOLUME_LEVEL,
    ATTR_MEDIA_VOLUME_MUTED,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    SERVICE_CLEAR_PLAYLIST,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
    BrowseError,
    MediaClass,
    MediaPlayerEnqueue,
    MediaPlayerEntityFeature,
    MediaType,
)
from homeassistant.components.openhome.const import (
    DOMAIN,
    OpenhomeService,
    OpenhomeServiceArgument,
)
from homeassistant.components.openhome.media_player import PLAYLIST_MEDIA_CONTENT_ID
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_NEXT_TRACK,
    SERVICE_MEDIA_PAUSE,
    SERVICE_MEDIA_PLAY,
    SERVICE_MEDIA_PREVIOUS_TRACK,
    SERVICE_MEDIA_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
    CommonServiceArgument,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from . import async_poll, setup_integration
from .conftest import TRACK_INFO

ENTITY_ID = "media_player.friendly_name"
MEDIA_ID = "http://localhost/track.flac"
TRACK_METADATA = didl_lite.generate_string({"title": "Home Assistant", "uri": MEDIA_ID})

# Each action, the coroutine it drives, a source type that exposes the supported
# feature it is gated behind, and the arguments the device must be called with.
ACTIONS = [
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_TURN_ON,
        {},
        Device.set_standby,
        "Playlist",
        (False,),
        id="turn_on",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_TURN_OFF,
        {},
        Device.set_standby,
        "Playlist",
        (True,),
        id="turn_off",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PLAY,
        {},
        Device.play,
        "Playlist",
        (),
        id="media_play",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PAUSE,
        {},
        Device.pause,
        "Playlist",
        (),
        id="media_pause",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_STOP,
        {},
        Device.stop,
        "Radio",
        (),
        id="media_stop",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_NEXT_TRACK,
        {},
        Device.skip,
        "Playlist",
        (1,),
        id="media_next_track",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_MEDIA_PREVIOUS_TRACK,
        {},
        Device.skip,
        "Playlist",
        (-1,),
        id="media_previous_track",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_UP,
        {},
        Device.increase_volume,
        "Playlist",
        (),
        id="volume_up",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_DOWN,
        {},
        Device.decrease_volume,
        "Playlist",
        (),
        id="volume_down",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_SET,
        {ATTR_MEDIA_VOLUME_LEVEL: 0.5},
        Device.set_volume,
        "Playlist",
        (50,),
        id="volume_set",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_MUTE,
        {ATTR_MEDIA_VOLUME_MUTED: True},
        Device.set_mute,
        "Playlist",
        (True,),
        id="volume_mute",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_VOLUME_MUTE,
        {ATTR_MEDIA_VOLUME_MUTED: False},
        Device.set_mute,
        "Playlist",
        (False,),
        id="volume_unmute",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_SELECT_SOURCE,
        {ATTR_INPUT_SOURCE: "Radio"},
        Device.set_source,
        "Playlist",
        (1,),
        id="select_source",
    ),
    pytest.param(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
            ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
        },
        Device.play_media,
        "Radio",
        ({"title": "Home Assistant", "uri": MEDIA_ID},),
        id="play_media",
    ),
]

# Each enqueue mode and the Playlist service actions it must drive: ADD appends
# after the last track id, NEXT inserts after the current id and seeks to it,
# PLAY appends and seeks to the new track, REPLACE clears the playlist first.
ENQUEUE_PLAYLIST_CALLS = [
    pytest.param(
        MediaPlayerEnqueue.ADD,
        {
            "IdArray": {},
            "Insert": {"AfterId": 3, "Uri": MEDIA_ID, "Metadata": TRACK_METADATA},
        },
        id="add",
    ),
    pytest.param(
        MediaPlayerEnqueue.NEXT,
        {
            "Id": {},
            "Insert": {"AfterId": 2, "Uri": MEDIA_ID, "Metadata": TRACK_METADATA},
            "SeekId": {"Value": 4},
        },
        id="next",
    ),
    pytest.param(
        MediaPlayerEnqueue.PLAY,
        {
            "IdArray": {},
            "Insert": {"AfterId": 3, "Uri": MEDIA_ID, "Metadata": TRACK_METADATA},
            "SeekId": {"Value": 4},
        },
        id="play",
    ),
    pytest.param(
        MediaPlayerEnqueue.REPLACE,
        {
            "DeleteAll": {},
            "Insert": {"AfterId": 0, "Uri": MEDIA_ID, "Metadata": TRACK_METADATA},
            "SeekId": {"Value": 4},
        },
        id="replace",
    ),
]


@pytest.fixture(autouse=True)
def media_proxy_token() -> Generator[None]:
    """Freeze the media proxy token, which otherwise varies per run."""
    with patch("secrets.token_hex", return_value="mock_token"):
        yield


@pytest.fixture
def platforms() -> list[Platform]:
    """Only load the media player platform."""
    return [Platform.MEDIA_PLAYER]


async def setup_media_player(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Load the media player platform and poll once."""
    await setup_integration(hass, mock_config_entry)

    # Supported features are only set once the device has been polled.
    await async_poll(hass)


@pytest.mark.parametrize(
    ("domain", "service", "data", "method", "source_type", "expected_args"), ACTIONS
)
async def test_action_calls_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    domain: str,
    service: str,
    data: dict[str, Any],
    method: Callable[..., Any],
    source_type: str,
    expected_args: tuple[Any, ...],
) -> None:
    """Test every action calls the device with the arguments it expects."""
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )

    getattr(mock_device, method.__name__).assert_awaited_once_with(*expected_args)


@pytest.mark.parametrize(
    ("domain", "service", "data", "method", "source_type", "expected_args"), ACTIONS
)
async def test_action_error_is_raised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    domain: str,
    service: str,
    data: dict[str, Any],
    method: Callable[..., Any],
    source_type: str,
    expected_args: tuple[Any, ...],
) -> None:
    """Test every action raises when the device rejects the request."""
    # The feature each action is gated behind depends on the selected source.
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    mocked = getattr(mock_device, method.__name__)
    mocked.side_effect = OpenhomeConnectionError("no route to host")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
        )

    # The message is keyed on the service name, so each action reports its own.
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == service
    mocked.assert_awaited()


@pytest.mark.parametrize(("enqueue", "expected_calls"), ENQUEUE_PLAYLIST_CALLS)
async def test_play_media_enqueue_on_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    enqueue: MediaPlayerEnqueue,
    expected_calls: dict[str, dict[str, Any]],
) -> None:
    """Test each enqueue mode drives the expected Playlist service actions."""
    mock_device.source.return_value = {
        "index": 0,
        "name": "Playlist",
        "type": "Playlist",
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY_ID,
            ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
            ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
            ATTR_MEDIA_ENQUEUE: enqueue,
        },
        blocking=True,
    )

    assert set(mock_device.playlist_actions) == set(expected_calls)
    for name, kwargs in expected_calls.items():
        mock_device.playlist_actions[name].async_call.assert_awaited_once_with(**kwargs)
    mock_device.play_media.assert_not_awaited()


async def test_play_media_enqueue_ignored_off_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test enqueue falls back to play_media when the source has no playlist."""
    mock_device.source.return_value = {
        "index": 1,
        "name": "Radio",
        "type": "Radio",
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY_ID,
            ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
            ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
            ATTR_MEDIA_ENQUEUE: MediaPlayerEnqueue.ADD,
        },
        blocking=True,
    )

    mock_device.play_media.assert_awaited_once_with(
        {"title": "Home Assistant", "uri": MEDIA_ID}
    )
    assert not mock_device.playlist_actions


async def test_play_media_enqueue_error_is_raised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test a rejected Playlist service action raises HomeAssistantError."""
    mock_device.source.return_value = {
        "index": 0,
        "name": "Playlist",
        "type": "Playlist",
    }

    await setup_media_player(hass, mock_config_entry)

    delete_all = mock_device.playlist_service.action("DeleteAll")
    delete_all.async_call.side_effect = UpnpError("SOAP fault")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            MEDIA_PLAYER_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: ENTITY_ID,
                ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
                ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
                ATTR_MEDIA_ENQUEUE: MediaPlayerEnqueue.REPLACE,
            },
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == SERVICE_PLAY_MEDIA
    delete_all.async_call.assert_awaited_once_with()


async def test_play_media_without_enqueue_appends_to_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test play_media without an explicit enqueue appends and plays on the playlist."""
    mock_device.source.return_value = {
        "index": 0,
        "name": "Playlist",
        "type": "Playlist",
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY_ID,
            ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
            ATTR_MEDIA_CONTENT_ID: MEDIA_ID,
        },
        blocking=True,
    )

    actions = mock_device.playlist_actions
    assert set(actions) == {"IdArray", "Insert", "SeekId"}
    actions["Insert"].async_call.assert_awaited_once_with(
        AfterId=3, Uri=MEDIA_ID, Metadata=TRACK_METADATA
    )
    actions["SeekId"].async_call.assert_awaited_once_with(Value=4)
    mock_device.play_media.assert_not_awaited()


@pytest.mark.parametrize(
    ("media_content_id", "media_content_type", "expected_calls"),
    [
        pytest.param(
            PLAYLIST_MEDIA_CONTENT_ID,
            MediaType.PLAYLIST,
            {"Play": {}},
            id="playlist",
        ),
        pytest.param(
            f"{PLAYLIST_MEDIA_CONTENT_ID}/2",
            MediaType.MUSIC,
            {"SeekId": {"Value": 2}},
            id="track",
        ),
    ],
)
async def test_play_media_playlist_reference(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    media_content_id: str,
    media_content_type: MediaType,
    expected_calls: dict[str, dict[str, Any]],
) -> None:
    """Test playing a browsed playlist item plays the playlist or seeks to the track."""
    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: ENTITY_ID,
            ATTR_MEDIA_CONTENT_TYPE: media_content_type,
            ATTR_MEDIA_CONTENT_ID: media_content_id,
        },
        blocking=True,
    )

    assert set(mock_device.playlist_actions) == set(expected_calls)
    for name, kwargs in expected_calls.items():
        mock_device.playlist_actions[name].async_call.assert_awaited_once_with(**kwargs)
    mock_device.play_media.assert_not_awaited()


async def test_browse_media_root_lists_device_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test the media browser root offers the device playlist."""
    await async_setup_component(hass, "media_source", {})
    await setup_media_player(hass, mock_config_entry)

    player = hass.data[MEDIA_PLAYER_DOMAIN].get_entity(ENTITY_ID)
    assert player is not None
    result = await player.async_browse_media()

    nodes = [
        child
        for child in result.children or []
        if child.media_content_id == PLAYLIST_MEDIA_CONTENT_ID
    ]
    assert len(nodes) == 1
    assert nodes[0].media_class == MediaClass.PLAYLIST
    assert nodes[0].can_play
    assert nodes[0].can_expand


async def test_browse_media_playlist_lists_tracks(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test browsing the device playlist lists its tracks with their ids."""
    await setup_media_player(hass, mock_config_entry)

    player = hass.data[MEDIA_PLAYER_DOMAIN].get_entity(ENTITY_ID)
    assert player is not None
    result = await player.async_browse_media(None, PLAYLIST_MEDIA_CONTENT_ID)

    assert result.media_content_id == PLAYLIST_MEDIA_CONTENT_ID
    assert result.media_class == MediaClass.PLAYLIST
    children = result.children or []
    assert [child.media_content_id for child in children] == [
        f"{PLAYLIST_MEDIA_CONTENT_ID}/{track_id}" for track_id in (1, 2, 3)
    ]
    assert [child.title for child in children] == ["Track 1", "Track 2", "Track 3"]
    assert all(child.can_play and not child.can_expand for child in children)
    mock_device.playlist_actions["ReadList"].async_call.assert_awaited_once_with(
        IdList="1 2 3"
    )


async def test_browse_media_playlist_error_is_raised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test a rejected Playlist service action while browsing raises BrowseError."""
    await setup_media_player(hass, mock_config_entry)

    id_array = mock_device.playlist_service.action("IdArray")
    id_array.async_call.side_effect = OpenhomeConnectionError("no route to host")

    player = hass.data[MEDIA_PLAYER_DOMAIN].get_entity(ENTITY_ID)
    assert player is not None
    with pytest.raises(BrowseError) as err:
        await player.async_browse_media(None, PLAYLIST_MEDIA_CONTENT_ID)

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "browse_playlist_failed"


async def test_clear_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test clear_playlist empties the device playlist."""
    mock_device.source.return_value = {
        "index": 0,
        "name": "Playlist",
        "type": "Playlist",
    }

    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        MEDIA_PLAYER_DOMAIN,
        SERVICE_CLEAR_PLAYLIST,
        {ATTR_ENTITY_ID: ENTITY_ID},
        blocking=True,
    )

    mock_device.playlist_actions["DeleteAll"].async_call.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("data", "expected_ids"),
    [
        pytest.param({OpenhomeServiceArgument.TRACK_ID: 2}, [2], id="single_id"),
        pytest.param(
            {OpenhomeServiceArgument.TRACK_ID: [1, 3]}, [1, 3], id="multiple_ids"
        ),
        pytest.param(
            {OpenhomeServiceArgument.URI: "http://localhost/track2.flac"},
            [2],
            id="uri",
        ),
        pytest.param(
            {
                OpenhomeServiceArgument.URI: [
                    "http://localhost/track1.flac",
                    "http://localhost/track3.flac",
                ]
            },
            [1, 3],
            id="multiple_uris",
        ),
        pytest.param(
            {
                OpenhomeServiceArgument.TRACK_ID: 1,
                OpenhomeServiceArgument.URI: "http://localhost/track3.flac",
            },
            [1, 3],
            id="id_and_uri",
        ),
    ],
)
async def test_remove_from_playlist(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    data: dict[str, Any],
    expected_ids: list[int],
) -> None:
    """Test remove_from_playlist deletes the tracks by id or uri."""
    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        OpenhomeService.REMOVE_FROM_PLAYLIST,
        {ATTR_ENTITY_ID: ENTITY_ID, **data},
        blocking=True,
    )

    delete_id = mock_device.playlist_actions["DeleteId"]
    delete_id.async_call.assert_has_awaits(
        [call(Id=track_id) for track_id in expected_ids]
    )
    if OpenhomeServiceArgument.URI in data:
        mock_device.playlist_actions["ReadList"].async_call.assert_awaited_once_with(
            IdList="1 2 3"
        )


async def test_remove_from_playlist_requires_a_track(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test remove_from_playlist without a track id or uri is rejected."""
    await setup_media_player(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            OpenhomeService.REMOVE_FROM_PLAYLIST,
            {ATTR_ENTITY_ID: ENTITY_ID},
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "remove_from_playlist_no_tracks"
    assert "DeleteId" not in mock_device.playlist_actions


async def test_remove_from_playlist_error_is_raised(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
) -> None:
    """Test a rejected DeleteId raises HomeAssistantError."""
    await setup_media_player(hass, mock_config_entry)

    delete_id = mock_device.playlist_service.action("DeleteId")
    delete_id.async_call.side_effect = UpnpError("SOAP fault")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            OpenhomeService.REMOVE_FROM_PLAYLIST,
            {ATTR_ENTITY_ID: ENTITY_ID, OpenhomeServiceArgument.TRACK_ID: 2},
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == OpenhomeService.REMOVE_FROM_PLAYLIST
    delete_id.async_call.assert_awaited_once_with(Id=2)


@pytest.mark.parametrize(
    ("source_type", "expect_enqueue"),
    [
        pytest.param("Playlist", True, id="playlist_source"),
        pytest.param("Radio", False, id="radio_source"),
    ],
)
async def test_media_enqueue_feature(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    source_type: str,
    expect_enqueue: bool,
) -> None:
    """Test MEDIA_ENQUEUE is only advertised for playlist sources."""
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    features = hass.states.get(ENTITY_ID).attributes["supported_features"]
    assert bool(features & MediaPlayerEntityFeature.MEDIA_ENQUEUE) is expect_enqueue


@pytest.mark.parametrize(
    ("source_type", "expect_clear"),
    [
        pytest.param("Playlist", True, id="playlist_source"),
        pytest.param("Radio", False, id="radio_source"),
    ],
)
async def test_clear_playlist_feature(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    source_type: str,
    expect_clear: bool,
) -> None:
    """Test CLEAR_PLAYLIST is only advertised for playlist sources."""
    mock_device.source.return_value = {
        "index": 0,
        "name": source_type,
        "type": source_type,
    }

    await setup_media_player(hass, mock_config_entry)

    features = hass.states.get(ENTITY_ID).attributes["supported_features"]
    assert bool(features & MediaPlayerEntityFeature.CLEAR_PLAYLIST) is expect_clear


async def test_invoke_pin_calls_device(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_device: MagicMock
) -> None:
    """Test the invoke_pin action passes the requested pin to the device."""
    await setup_media_player(hass, mock_config_entry)

    await hass.services.async_call(
        DOMAIN,
        OpenhomeService.INVOKE_PIN,
        {CommonServiceArgument.ENTITY_ID: ENTITY_ID, OpenhomeServiceArgument.PIN: 3},
        blocking=True,
    )

    mock_device.invoke_pin.assert_awaited_once_with(3)


@pytest.mark.parametrize(
    ("pins_enabled", "translation_key", "await_count"),
    [
        pytest.param(True, "invoke_pin", 1, id="pins_supported"),
        pytest.param(False, "pins_not_supported", 0, id="pins_not_supported"),
    ],
)
async def test_invoke_pin(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    pins_enabled: bool,
    translation_key: str,
    await_count: int,
) -> None:
    """Test invoking a pin on a device with and without pin support."""
    mock_device.pins_enabled = pins_enabled

    await setup_media_player(hass, mock_config_entry)
    mock_device.invoke_pin.side_effect = OpenhomeConnectionError("no route to host")

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            OpenhomeService.INVOKE_PIN,
            {
                CommonServiceArgument.ENTITY_ID: ENTITY_ID,
                OpenhomeServiceArgument.PIN: 1,
            },
            blocking=True,
        )

    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == translation_key
    assert mock_device.invoke_pin.await_count == await_count


async def test_media_player(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
) -> None:
    """Test the media player is set up from the device state."""
    mock_device.track_info.return_value = TRACK_INFO

    await setup_media_player(hass, mock_config_entry)

    await snapshot_platform(hass, entity_registry, snapshot, mock_config_entry.entry_id)


async def test_becomes_unavailable_and_recovers(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failed poll logs once, marks the device unavailable and recovers."""
    await setup_media_player(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == STATE_PLAYING

    mock_device.room.side_effect = OpenhomeConnectionError("device unreachable")
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert caplog.text.count("device unreachable") == 1

    # A second consecutive failure must not log the same outage again.
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert caplog.text.count("device unreachable") == 1

    mock_device.room.side_effect = None
    await async_poll(hass)

    assert hass.states.get(ENTITY_ID).state == STATE_PLAYING


@pytest.mark.parametrize(
    ("in_standby", "transport_state", "expected_state"),
    [
        pytest.param(True, "Playing", STATE_OFF, id="standby"),
        pytest.param(False, "Paused", STATE_PAUSED, id="paused"),
        pytest.param(False, "Playing", STATE_PLAYING, id="playing"),
        pytest.param(False, "Buffering", STATE_PLAYING, id="buffering"),
        pytest.param(False, "Stopped", STATE_IDLE, id="stopped"),
        # An external source with no transport controls still counts as playing.
        pytest.param(False, "Unknown", STATE_PLAYING, id="external_source"),
    ],
)
async def test_state_mapping(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device: MagicMock,
    in_standby: bool,
    transport_state: str,
    expected_state: str,
) -> None:
    """Test the device transport state maps onto the media player state."""
    mock_device.is_in_standby.return_value = in_standby
    mock_device.transport_state.return_value = transport_state

    await setup_media_player(hass, mock_config_entry)

    assert hass.states.get(ENTITY_ID).state == expected_state
