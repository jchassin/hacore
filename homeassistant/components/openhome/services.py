"""Support for Openhome Devices."""

import probatio

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, service

from .const import DOMAIN, OpenhomeService, OpenhomeServiceArgument


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        OpenhomeService.INVOKE_PIN,
        entity_domain=MEDIA_PLAYER_DOMAIN,
        schema={probatio.Required(OpenhomeServiceArgument.PIN): cv.positive_int},
        func="async_invoke_pin",
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        OpenhomeService.REMOVE_FROM_PLAYLIST,
        entity_domain=MEDIA_PLAYER_DOMAIN,
        schema={
            probatio.Optional(OpenhomeServiceArgument.TRACK_ID): probatio.Any(
                cv.positive_int, [cv.positive_int]
            ),
            probatio.Optional(OpenhomeServiceArgument.URI): probatio.Any(
                cv.string, [cv.string]
            ),
        },
        func="async_remove_from_playlist",
    )
