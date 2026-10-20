"""Select inference transport and credential lifecycle independently."""
from providers.corporate import CorporateProviderAdapter
from providers.openai_compatible import OpenAICompatibleAdapter
from providers.openrouter import OpenRouterAdapter


def providers(settings, client):
    s = settings
    if s.provider_mode == "corporate":
        adapter = CorporateProviderAdapter()
        return adapter, adapter
    if s.provider_mode == "openai-compatible":
        catalog_key = s.provider_catalog_api_key.get_secret_value()
        # A shared inference key can authenticate /models on the SAME configured base only.
        # A separate catalog host/base needs its own explicit credential.
        if not catalog_key and s.provider_credential_mode == "static" and (
                not s.provider_catalog_base_url or s.provider_catalog_base_url == s.provider_inference_base_url):
            catalog_key = s.provider_api_key.get_secret_value()
        inference = OpenAICompatibleAdapter(client, s.provider_inference_base_url,
            catalog_base=s.provider_catalog_base_url or None, catalog_key=catalog_key,
            models_path=s.provider_models_path, chat_path=s.provider_chat_path)
        if s.provider_credential_mode != "openrouter-managed":
            return inference, None
    else:
        inference = None
    manager = OpenRouterAdapter(client,
        s.fake_provider_url if s.provider_mode == "fake" else s.openrouter_base_url,
        s.fake_management_key.get_secret_value() if s.provider_mode == "fake" else s.openrouter_management_key.get_secret_value(),
        **s.provider_options)
    return inference or manager, manager
