"""Support for Openhome Devices."""

import base64
from collections.abc import Awaitable, Callable, Coroutine
import functools
import logging
import struct
from typing import Any, Concatenate, override

from async_upnp_client.exceptions import UpnpError
import defusedxml.ElementTree as ET
from openhomedevice import didl_lite
from openhomedevice.exceptions import OpenhomeError

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    ATTR_MEDIA_ENQUEUE,
    SERVICE_CLEAR_PLAYLIST,
    SERVICE_PLAY_MEDIA,
    SERVICE_SELECT_SOURCE,
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEnqueue,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_source import MediaSourceItem
from homeassistant.const import (
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
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenhomeConfigEntry
from .const import DOMAIN, OpenhomeService

SUPPORT_OPENHOME = (
    MediaPlayerEntityFeature.SELECT_SOURCE
    | MediaPlayerEntityFeature.TURN_OFF
    | MediaPlayerEntityFeature.TURN_ON
)

# media_content_id of the device playlist node in the media browser; a track of
# that playlist is addressed as f"{PLAYLIST_MEDIA_CONTENT_ID}/{track_id}".
PLAYLIST_MEDIA_CONTENT_ID = "openhome://playlist"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OpenhomeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Openhome config entry."""

    _LOGGER.debug("Setting up config entry: %s", config_entry.unique_id)

    device = config_entry.runtime_data

    entity = OpenhomeDevice(device)

    async_add_entities([entity])


type _FuncType[_T, **_P, _R] = Callable[Concatenate[_T, _P], Awaitable[_R]]
type _ReturnFuncType[_T, **_P, _R] = Callable[
    Concatenate[_T, _P], Coroutine[Any, Any, _R]
]


def catch_request_errors[_OpenhomeDeviceT: OpenhomeDevice, **_P, _R](
    action: str,
) -> Callable[
    [_FuncType[_OpenhomeDeviceT, _P, _R]], _ReturnFuncType[_OpenhomeDeviceT, _P, _R]
]:
    """Return decorator that catches errors and raises HomeAssistantError."""

    def call_wrapper(
        func: _FuncType[_OpenhomeDeviceT, _P, _R],
    ) -> _ReturnFuncType[_OpenhomeDeviceT, _P, _R]:
        """Call wrapper for decorator."""

        @functools.wraps(func)
        async def wrapper(
            self: _OpenhomeDeviceT, *args: _P.args, **kwargs: _P.kwargs
        ) -> _R:
            """Catch device errors, including raw network OSErrors."""
            try:
                return await func(self, *args, **kwargs)
            except (OpenhomeError, UpnpError, OSError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=action,
                ) from err

        return wrapper

    return call_wrapper


class OpenhomeDevice(MediaPlayerEntity):
    """Representation of an Openhome device."""

    _attr_supported_features = SUPPORT_OPENHOME
    _attr_state = MediaPlayerState.PLAYING
    _attr_available = True

    def __init__(self, device):
        """Initialise the Openhome device."""
        self._device = device
        self._attr_unique_id = device.uuid()
        self._source_index = {}
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, device.uuid()),
            },
            manufacturer=device.manufacturer(),
            model=device.model_name(),
            name=device.friendly_name(),
        )

    async def async_update(self) -> None:
        """Update state of device."""
        try:
            self._attr_name = await self._device.room()
            self._attr_supported_features = SUPPORT_OPENHOME
            source_index = {}
            source_names = []

            track_information = await self._device.track_info()
            self._attr_media_image_url = track_information.get("albumArtwork")
            self._attr_media_album_name = track_information.get("albumTitle")
            self._attr_media_title = track_information.get("title")
            if artists := track_information.get("artist"):
                self._attr_media_artist = artists[0]
            self._attr_media_content_id = track_information.get("uri")
            self._attr_media_content_type = MediaType.MUSIC

            if self._device.volume_enabled:
                self._attr_supported_features |= (
                    MediaPlayerEntityFeature.VOLUME_STEP
                    | MediaPlayerEntityFeature.VOLUME_MUTE
                    | MediaPlayerEntityFeature.VOLUME_SET
                )
                self._attr_volume_level = await self._device.volume() / 100.0
                self._attr_is_volume_muted = await self._device.is_muted()

            for source in await self._device.sources():
                source_names.append(source["name"])
                source_index[source["name"]] = source["index"]

            source = await self._device.source()
            self._attr_source = source.get("name")
            self._source_index = source_index
            self._attr_source_list = source_names

            if source["type"] in ("Radio", "Receiver"):
                self._attr_supported_features |= (
                    MediaPlayerEntityFeature.STOP
                    | MediaPlayerEntityFeature.PLAY
                    | MediaPlayerEntityFeature.PLAY_MEDIA
                    | MediaPlayerEntityFeature.BROWSE_MEDIA
                )
            if source["type"] in ("Playlist", "Spotify"):
                self._attr_supported_features |= (
                    MediaPlayerEntityFeature.PREVIOUS_TRACK
                    | MediaPlayerEntityFeature.NEXT_TRACK
                    | MediaPlayerEntityFeature.PAUSE
                    | MediaPlayerEntityFeature.PLAY
                    | MediaPlayerEntityFeature.PLAY_MEDIA
                    | MediaPlayerEntityFeature.BROWSE_MEDIA
                    | MediaPlayerEntityFeature.MEDIA_ENQUEUE
                    | MediaPlayerEntityFeature.CLEAR_PLAYLIST
                )

            in_standby = await self._device.is_in_standby()
            transport_state = await self._device.transport_state()
            if in_standby:
                self._attr_state = MediaPlayerState.OFF
            elif transport_state == "Paused":
                self._attr_state = MediaPlayerState.PAUSED
            elif transport_state in ("Playing", "Buffering"):
                self._attr_state = MediaPlayerState.PLAYING
            elif transport_state in ("Stopped", "Waiting"):
                self._attr_state = MediaPlayerState.IDLE
            else:
                # Device is playing an external source with no transport controls
                self._attr_state = MediaPlayerState.PLAYING

            self._attr_available = True
        except (OpenhomeError, OSError) as err:
            if self._attr_available:
                _LOGGER.warning("Error updating %s: %s", self.entity_id, err)
            self._attr_available = False

    @catch_request_errors(SERVICE_TURN_ON)
    @override
    async def async_turn_on(self) -> None:
        """Bring device out of standby."""
        await self._device.set_standby(False)

    @catch_request_errors(SERVICE_TURN_OFF)
    @override
    async def async_turn_off(self) -> None:
        """Put device in standby."""
        await self._device.set_standby(True)

    @catch_request_errors(SERVICE_PLAY_MEDIA)
    @override
    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Send the play_media command to the media player."""
        if media_id == PLAYLIST_MEDIA_CONTENT_ID or media_id.startswith(
            f"{PLAYLIST_MEDIA_CONTENT_ID}/"
        ):
            await self._async_play_playlist_media(media_id)
            return

        # Radio streams carry their mime type as media_content_type; they are
        # played through the Openhome Radio service, not added to the playlist.
        is_radio = media_type.startswith("audio/")
        track_details = {"title": "Home Assistant", "uri": media_id}

        if media_source.is_media_source_id(media_id):
            source_item = MediaSourceItem.from_uri(self.hass, media_id, self.entity_id)
            is_radio = source_item.domain == "radio_browser"
            media_type = MediaType.MUSIC
            browsed = await self._async_browse_media_item(media_id)
            play_item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = play_item.url
            # Keep the media's own metadata instead of a generic one.
            track_details = {
                "title": "Home Assistant",
                "uri": media_id,
                "albumArtwork": "",
            }
            if browsed is not None:
                track_details["title"] = browsed.title or "Home Assistant"
                track_details["albumArtwork"] = browsed.thumbnail or ""

        if media_type != MediaType.MUSIC and not is_radio:
            _LOGGER.error(
                "Invalid media type %s. Only %s and audio streams are supported",
                media_type,
                MediaType.MUSIC,
            )
            return

        media_id = async_process_play_media_url(self.hass, media_id)
        track_details["uri"] = media_id

        if is_radio:
            await self._device.play_media(track_details)
            return

        # Without an explicit enqueue, playing media appends it to the device
        # playlist and plays it there instead of streaming it directly.
        enqueue = kwargs.get(ATTR_MEDIA_ENQUEUE, MediaPlayerEnqueue.PLAY)

        source = await self._device.source()
        if source["type"] in ("Playlist", "Spotify"):
            await self._async_play_on_playlist(track_details, enqueue)
        else:
            await self._device.play_media(track_details)

    async def _async_playlist_action(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Call an action on the device Playlist service."""
        action = self._device.playlist_service.action(name)
        return await action.async_call(**kwargs)

    async def _async_playlist_ids(self) -> list[int]:
        """Return the ids of the tracks in the device playlist."""
        result = await self._async_playlist_action("IdArray")
        data = base64.b64decode(result.get("Array", ""))
        if not data:
            return []
        return list(struct.unpack(f">{len(data) // 4}I", data))

    async def _async_last_track_id(self) -> int:
        """Return the id of the last track in the playlist, 0 when empty."""
        ids = await self._async_playlist_ids()
        return ids[-1] if ids else 0

    async def _async_playlist_tracks(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Return the uri and metadata of the given playlist tracks, by id."""
        if not ids:
            return {}
        result = await self._async_playlist_action(
            "ReadList", IdList=" ".join(str(track_id) for track_id in ids)
        )
        track_list = result.get("TrackList", "")
        if not track_list:
            return {}
        tracks: dict[int, dict[str, Any]] = {}
        for entry in ET.fromstring(track_list).findall("Entry"):
            track_id = int(entry.findtext("Id", "0"))
            details = didl_lite.parse(entry.findtext("Metadata"))
            details["uri"] = entry.findtext("Uri") or details.get("uri", "")
            tracks[track_id] = details
        return tracks

    async def _async_play_playlist_media(self, media_content_id: str) -> None:
        """Play the device playlist itself or one of its tracks."""
        if media_content_id == PLAYLIST_MEDIA_CONTENT_ID:
            await self._async_playlist_action("Play")
            return
        track_id = media_content_id.removeprefix(f"{PLAYLIST_MEDIA_CONTENT_ID}/")
        await self._async_playlist_action("SeekId", Value=int(track_id))

    async def _async_browse_media_item(
        self, media_content_id: str
    ) -> BrowseMedia | None:
        """Return the browse item for a media_content_id.

        Browsing a leaf id can return a listing instead of the item (e.g.
        radio_browser returns an empty listing for a station id), so fall back
        to searching the media source root.
        """
        item = await media_source.async_browse_media(self.hass, media_content_id)
        if not item.can_expand:
            return item
        parsed = MediaSourceItem.from_uri(self.hass, media_content_id, None)
        if parsed.domain is None:
            return None
        root = await media_source.async_browse_media(
            self.hass, f"{media_source.URI_SCHEME}{parsed.domain}"
        )
        return await self._async_search_media_tree(root, media_content_id, depth=0)

    async def _async_search_media_tree(
        self, item: BrowseMedia, media_content_id: str, *, depth: int
    ) -> BrowseMedia | None:
        """Search the children of an expandable browse item for a media_content_id."""
        if depth > 2:
            return None
        for child in item.children or []:
            if child.media_content_id == media_content_id:
                return child
            if child.can_expand:
                sub_item = await media_source.async_browse_media(
                    self.hass, child.media_content_id
                )
                found = await self._async_search_media_tree(
                    sub_item, media_content_id, depth=depth + 1
                )
                if found is not None:
                    return found
        return None

    async def _async_insert_track(
        self, track_details: dict[str, str], after_id: int
    ) -> int:
        """Insert a track in the playlist after after_id, returning its new id."""
        result = await self._async_playlist_action(
            "Insert",
            AfterId=after_id,
            Uri=track_details["uri"],
            Metadata=didl_lite.generate_string(track_details),
        )
        return int(result["NewId"])

    async def _async_play_on_playlist(
        self, track_details: dict[str, str], enqueue: MediaPlayerEnqueue
    ) -> None:
        """Add the track to the device playlist according to the enqueue mode."""
        if enqueue == MediaPlayerEnqueue.ADD:
            await self._async_insert_track(
                track_details, await self._async_last_track_id()
            )
        elif enqueue == MediaPlayerEnqueue.NEXT:
            current_id = (await self._async_playlist_action("Id"))["Value"]
            new_id = await self._async_insert_track(track_details, current_id)
            await self._async_playlist_action("SeekId", Value=new_id)
        elif enqueue == MediaPlayerEnqueue.PLAY:
            new_id = await self._async_insert_track(
                track_details, await self._async_last_track_id()
            )
            await self._async_playlist_action("SeekId", Value=new_id)
        else:
            await self._async_playlist_action("DeleteAll")
            new_id = await self._async_insert_track(track_details, 0)
            await self._async_playlist_action("SeekId", Value=new_id)

    @catch_request_errors(SERVICE_MEDIA_PAUSE)
    @override
    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self._device.pause()

    @catch_request_errors(SERVICE_MEDIA_STOP)
    @override
    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self._device.stop()

    @catch_request_errors(SERVICE_MEDIA_PLAY)
    @override
    async def async_media_play(self) -> None:
        """Send play command."""
        await self._device.play()

    @catch_request_errors(SERVICE_MEDIA_NEXT_TRACK)
    @override
    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self._device.skip(1)

    @catch_request_errors(SERVICE_MEDIA_PREVIOUS_TRACK)
    @override
    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self._device.skip(-1)

    @catch_request_errors(SERVICE_SELECT_SOURCE)
    @override
    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        await self._device.set_source(self._source_index[source])

    @catch_request_errors(SERVICE_CLEAR_PLAYLIST)
    @override
    async def async_clear_playlist(self) -> None:
        """Clear the device playlist."""
        await self._async_playlist_action("DeleteAll")

    @catch_request_errors(OpenhomeService.REMOVE_FROM_PLAYLIST)
    async def async_remove_from_playlist(
        self,
        track_id: int | list[int] | None = None,
        uri: str | list[str] | None = None,
    ) -> None:
        """Remove tracks from the device playlist by id or uri."""
        if track_id is None and uri is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="remove_from_playlist_no_tracks",
            )
        ids = {track_id} if isinstance(track_id, int) else set(track_id or ())
        if uri:
            wanted = {uri} if isinstance(uri, str) else set(uri)
            tracks = await self._async_playlist_tracks(await self._async_playlist_ids())
            ids.update(
                track
                for track, details in tracks.items()
                if details.get("uri") in wanted
            )
        for track in sorted(ids):
            await self._async_playlist_action("DeleteId", Id=track)

    @catch_request_errors(OpenhomeService.INVOKE_PIN)
    async def async_invoke_pin(self, pin):
        """Invoke pin."""
        if not self._device.pins_enabled:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pins_not_supported",
            )
        await self._device.invoke_pin(pin)

    @catch_request_errors(SERVICE_VOLUME_UP)
    @override
    async def async_volume_up(self) -> None:
        """Volume up media player."""
        await self._device.increase_volume()

    @catch_request_errors(SERVICE_VOLUME_DOWN)
    @override
    async def async_volume_down(self) -> None:
        """Volume down media player."""
        await self._device.decrease_volume()

    @catch_request_errors(SERVICE_VOLUME_SET)
    @override
    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        await self._device.set_volume(int(volume * 100))

    @catch_request_errors(SERVICE_VOLUME_MUTE)
    @override
    async def async_mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        await self._device.set_mute(mute)

    @override
    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Implement the websocket media browsing helper."""
        if media_content_id is not None and (
            media_content_id == PLAYLIST_MEDIA_CONTENT_ID
            or media_content_id.startswith(f"{PLAYLIST_MEDIA_CONTENT_ID}/")
        ):
            return await self._async_browse_playlist()
        browse = await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
        if media_content_id is None:
            browse.children = [
                *(browse.children or []),
                BrowseMedia(
                    title="Device playlist",
                    media_class=MediaClass.PLAYLIST,
                    media_content_id=PLAYLIST_MEDIA_CONTENT_ID,
                    media_content_type=MediaType.PLAYLIST,
                    can_play=True,
                    can_expand=True,
                ),
            ]
        return browse

    async def _async_browse_playlist(self) -> BrowseMedia:
        """Browse the tracks of the device playlist."""
        try:
            ids = await self._async_playlist_ids()
            tracks = await self._async_playlist_tracks(ids)
        except (OpenhomeError, UpnpError, OSError) as err:
            raise BrowseError(
                translation_domain=DOMAIN,
                translation_key="browse_playlist_failed",
            ) from err
        return BrowseMedia(
            title="Device playlist",
            media_class=MediaClass.PLAYLIST,
            media_content_id=PLAYLIST_MEDIA_CONTENT_ID,
            media_content_type=MediaType.PLAYLIST,
            can_play=True,
            can_expand=True,
            children=[
                BrowseMedia(
                    title=tracks.get(track_id, {}).get("title") or f"Track {track_id}",
                    media_class=MediaClass.TRACK,
                    media_content_id=f"{PLAYLIST_MEDIA_CONTENT_ID}/{track_id}",
                    media_content_type=MediaType.MUSIC,
                    can_play=True,
                    can_expand=False,
                    thumbnail=tracks.get(track_id, {}).get("albumArtwork"),
                )
                for track_id in ids
            ],
            children_media_class=MediaClass.TRACK,
        )
