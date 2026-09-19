from __future__ import annotations

import keyring

from davincibot.models import ProviderKind

SERVICE_NAME = "DaVinciBot"


class SecretStore:
    def set_api_key(self, provider: ProviderKind, key: str) -> None:
        if provider is ProviderKind.OFFLINE:
            return
        keyring.set_password(SERVICE_NAME, provider.value, key)

    def get_api_key(self, provider: ProviderKind) -> str | None:
        if provider is ProviderKind.OFFLINE:
            return None
        return keyring.get_password(SERVICE_NAME, provider.value)

    def delete_api_key(self, provider: ProviderKind) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, provider.value)
        except keyring.errors.PasswordDeleteError:
            pass
