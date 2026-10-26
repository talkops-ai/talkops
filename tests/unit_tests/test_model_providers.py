# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for TalkOps 22-provider model catalog, spec resolution, and reasoning effort."""
from unittest.mock import MagicMock

import pytest
from textual.events import Click
from textual.message import Message
from textual.widgets import Input

from talkops.exceptions import ModelConfigError
from talkops.model.config import (
    NO_AUTH_REQUIRED_PROVIDERS,
    PROVIDER_API_KEY_ENV,
    detect_provider,
    format_token_count,
    get_provider_display_name,
    resolve_model_spec,
)
from talkops.model.factory import clear_model_cache, create_model
from talkops.model.reasoning import (
    default_effort_for_model,
    is_effort_supported_for_model,
    supported_efforts_for_model,
)

ALL_22_PROVIDERS = [
    "anthropic",
    "openai",
    "azure_openai",
    "google_genai",
    "google_vertexai",
    "bedrock",
    "groq",
    "deepseek",
    "openrouter",
    "ollama",
    "together",
    "fireworks",
    "mistralai",
    "cohere",
    "perplexity",
    "baseten",
    "huggingface",
    "nvidia",
    "xai",
    "ibm",
    "litellm",
    "meta",
]


def test_22_provider_catalog_completeness():
    """Verify 22 provider catalog completeness."""
    for provider in ALL_22_PROVIDERS:
        assert (
            provider in PROVIDER_API_KEY_ENV or provider in NO_AUTH_REQUIRED_PROVIDERS
        ), f"Missing API key or no-auth mapping for {provider}"
        display_name = get_provider_display_name(provider)
        assert display_name is not None and len(display_name) > 0, f"Missing display name for {provider}"


def test_resolve_model_spec():
    """Verify resolve model spec."""
    assert resolve_model_spec("anthropic:claude-3-5-sonnet") == ("anthropic", "claude-3-5-sonnet")
    assert resolve_model_spec("openai:gpt-4o") == ("openai", "gpt-4o")
    assert resolve_model_spec("bedrock:anthropic.claude-3-7-sonnet-20250219-v1:0") == (
        "bedrock",
        "anthropic.claude-3-7-sonnet-20250219-v1:0",
    )
    assert resolve_model_spec("deepseek:deepseek-chat") == ("deepseek", "deepseek-chat")
    assert resolve_model_spec("ollama:llama3.3") == ("ollama", "llama3.3")

    # Bare model name detection
    prov, m_id = resolve_model_spec("gpt-4o")
    assert prov == "openai"
    assert m_id == "gpt-4o"

    prov_c, m_c = resolve_model_spec("claude-3-5-sonnet")
    assert prov_c == "anthropic"

    # Empty or None spec does not default to any provider
    assert resolve_model_spec(None) == ("", "")
    assert resolve_model_spec("") == ("", "")


def test_detect_provider():
    """Verify detect provider."""
    assert detect_provider("gpt-4o") == "openai"
    assert detect_provider("claude-3-7-sonnet") == "anthropic"
    assert detect_provider("gemini-3.8-flash") == "google_genai"
    assert detect_provider("gemini-2.5-flash") == "google_genai"
    assert detect_provider("deepseek-chat") == "deepseek"
    assert detect_provider("mistral-large") == "mistralai"
    assert detect_provider("groq-something") == "groq"
    assert detect_provider("llama-3.3-70b-versatile") == "ollama"


def test_reasoning_effort_support():
    # OpenAI reasoning models
    """Verify reasoning effort support."""
    assert is_effort_supported_for_model("openai:o3-mini", "high") is True
    assert is_effort_supported_for_model("openai:o3-mini", "medium") is True
    assert is_effort_supported_for_model("openai:gpt-4o", "high") is False

    efforts = supported_efforts_for_model("openai:o3-mini")
    assert "low" in efforts and "medium" in efforts and "high" in efforts

    # Default effort check
    assert default_effort_for_model("openai:o3-mini") in ("medium", "high", "low")


def test_format_token_counts():
    """Verify format token counts."""
    assert format_token_count(1_000_000) == "1M"
    assert format_token_count(128_000) == "128k"
    assert format_token_count(8_192) == "8.2k"
    assert format_token_count(500) == "500"


def test_create_model_missing_credentials(monkeypatch, tmp_path):
    """Verify create model missing credentials."""
    from talkops.config import paths
    from talkops.config.settings import reset_settings_for_testing
    from talkops.model.factory import clear_model_cache

    clear_model_cache()
    reset_settings_for_testing()
    monkeypatch.setattr(paths, "GLOBAL_ENV_PATH", tmp_path / "nonexistent_env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALKOPS_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(Exception) as exc_info:
        create_model("anthropic:claude-3-5-sonnet")
    assert "credentials" in str(exc_info.value).lower() or "api_key" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


def test_create_model_ollama_local(monkeypatch):
    """Verify create model ollama local."""
    from talkops.exceptions import MissingProviderPackageError
    from talkops.model.factory import clear_model_cache

    clear_model_cache()
    # When package is missing, it raises MissingProviderPackageError; when present, it instantiates
    try:
        model = create_model("ollama:llama3.3")
        assert model is not None
    except MissingProviderPackageError as e:
        assert e.provider == "ollama"
        assert "langchain-ollama" in e.package

    clear_model_cache()
    # When mocked, it returns the model instance
    mock_model = MagicMock()
    monkeypatch.setattr("langchain.chat_models.init_chat_model", lambda *args, **kwargs: mock_model)
    res = create_model("ollama:llama3.3")
    assert res.model is mock_model
    assert res.provider == "ollama"
    clear_model_cache()


def test_bedrock_model_detection_and_resolution():
    """Verify bedrock model detection and resolution."""
    from talkops.model.config import is_bedrock_model_id, normalize_model_spec

    # Bare Bedrock IDs with version colons
    sonnet_v2 = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    us_sonnet = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
    llama_70b = "meta.llama3-3-70b-instruct-v1:0"
    nova_pro = "amazon.nova-pro-v1:0"

    assert is_bedrock_model_id(sonnet_v2) is True
    assert is_bedrock_model_id(us_sonnet) is True
    assert is_bedrock_model_id(llama_70b) is True
    assert is_bedrock_model_id(nova_pro) is True
    assert is_bedrock_model_id("gpt-4o") is False
    assert is_bedrock_model_id("claude-3-5-sonnet") is False

    # detect_provider
    assert detect_provider(sonnet_v2) == "bedrock"
    assert detect_provider(us_sonnet) == "bedrock"
    assert detect_provider(llama_70b) == "bedrock"
    assert detect_provider(nova_pro) == "bedrock"

    # resolve_model_spec does not break on version colon (:0)
    assert resolve_model_spec(sonnet_v2) == ("bedrock", sonnet_v2)
    assert resolve_model_spec(us_sonnet) == ("bedrock", us_sonnet)
    assert resolve_model_spec(f"bedrock:{sonnet_v2}") == ("bedrock", sonnet_v2)

    # normalize_model_spec
    assert normalize_model_spec(sonnet_v2) == f"bedrock:{sonnet_v2}"
    assert normalize_model_spec(f"bedrock:{sonnet_v2}") == f"bedrock:{sonnet_v2}"


def test_bedrock_auth_status_and_kwargs(monkeypatch):
    """Verify bedrock auth status and kwargs."""
    from talkops.model.config import ProviderAuthState, get_provider_auth_status
    from talkops.model.factory import _get_provider_kwargs

    # Clean env
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)

    # Incomplete credentials (access key without secret)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
    status = get_provider_auth_status("bedrock")
    assert status.state == ProviderAuthState.MISSING

    # Complete credentials
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_TEST")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    status = get_provider_auth_status("bedrock")
    assert status.state == ProviderAuthState.CONFIGURED

    # Verify provider kwargs injection
    kwargs = _get_provider_kwargs("bedrock", model_name="anthropic.claude-3-7-sonnet-20250219-v1:0")
    assert kwargs.get("region_name") == "eu-central-1"
    assert kwargs.get("aws_access_key_id") == "AKIA_TEST"
    assert kwargs.get("aws_secret_access_key") == "SECRET_TEST"
    assert "api_key" not in kwargs


def test_no_default_model_or_provider_without_user_selection(monkeypatch):
    """Ensure that the system never imposes a default model or provider on the user."""
    from talkops.config.settings import get_settings
    from talkops.exceptions import NoCredentialsConfiguredError
    from talkops.model.config import ModelConfig
    from talkops.model.factory import _get_default_model_spec

    # Clear any credentials that might be in env
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "GROQ_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "AWS_ACCESS_KEY_ID",
    ):
        monkeypatch.delenv(k, raising=False)

    # Empty settings without model
    s = get_settings()
    monkeypatch.setattr(s, "model_name", None)
    monkeypatch.setattr(s, "model", None)

    # ModelConfig.load() has no default forced
    monkeypatch.setattr("talkops.config.toml_config.load_default_model", lambda: None)
    monkeypatch.setattr("talkops.config.toml_config.load_recent_model", lambda: None)
    cfg = ModelConfig.load()
    assert cfg.default_model is None
    assert cfg.recent_model is None

    # has_provider_credentials returns False for key-based providers when keys are unset
    monkeypatch.setattr("talkops.model.factory.has_provider_credentials", lambda prov: False)

    # When no model is configured and no credentials exist, _get_default_model_spec raises
    with pytest.raises(NoCredentialsConfiguredError, match="No model is configured yet"):
        _get_default_model_spec()


def test_unified_model_config_consistency():
    """Verify unified model config consistency."""
    from talkops.model.config import (
        AVAILABLE_MODELS,
        DEFAULT_PROVIDER_PRIORITY,
        RECOMMENDED_SPECS,
        get_available_models_list,
        get_curated_models_for_provider,
    )

    # 1. DEFAULT_PROVIDER_PRIORITY covers all providers in AVAILABLE_MODELS
    for prov in AVAILABLE_MODELS:
        assert prov in DEFAULT_PROVIDER_PRIORITY, f"{prov} missing from DEFAULT_PROVIDER_PRIORITY"

    # 2. RECOMMENDED_SPECS includes "auto" and all curated models in AVAILABLE_MODELS
    assert "auto" in RECOMMENDED_SPECS
    for prov, models in AVAILABLE_MODELS.items():
        for model_id, _ in models:
            spec = f"{prov}:{model_id}"
            assert spec in RECOMMENDED_SPECS, f"{spec} missing from RECOMMENDED_SPECS"

    # 3. get_curated_models_for_provider returns all curated models
    genai_curated = get_curated_models_for_provider("google_genai")
    assert len(genai_curated) >= 10
    model_ids = [m[0] for m in genai_curated]
    assert "gemini-3.8-flash" in model_ids
    assert "gemini-3.7-flash" in model_ids
    assert "gemini-3.6-flash" in model_ids
    assert "gemini-3.5-flash" in model_ids
    assert "gemini-3.5-flash-lite" in model_ids
    assert not any(m_id.startswith("gemini-2.5") for m_id in model_ids)

    # 4. get_available_models_list has curated models at the beginning and excludes deprecated 2.5 series
    avail = get_available_models_list()
    avail_specs = [m[0] for m in avail]
    assert "google_genai:gemini-3.8-flash" in avail_specs
    assert "google_genai:gemini-3.7-flash" in avail_specs
    assert "google_genai:gemini-3.6-flash" in avail_specs
    assert "google_vertexai:gemini-3.8-flash" in avail_specs
    assert "openrouter:google/gemini-3.8-flash" in avail_specs
    assert "litellm:gemini/gemini-3.8-flash" in avail_specs
    assert not any(":gemini-2.5" in spec for spec in avail_specs)


def test_model_selector_prioritizes_active_and_shows_all_curated_models():
    """Verify model selector prioritizes active and shows all curated models."""
    from talkops.ui.widgets.model_selector import ModelSelectorScreen

    screen = ModelSelectorScreen(
        current_model="gemini-3.7-flash",
        current_provider="google_genai",
    )
    screen.on_mount()

    # Recommended view includes all curated models + auto
    assert screen._recommended_only is True
    filtered_specs = [m[0] for m in screen._filtered_models]
    assert "auto" in filtered_specs
    assert "google_genai:gemini-3.8-flash" in filtered_specs
    assert "google_genai:gemini-3.7-flash" in filtered_specs
    assert "google_genai:gemini-3.6-flash" in filtered_specs
    assert "google_genai:gemini-3.5-flash" in filtered_specs
    assert "google_genai:gemini-3.5-flash-lite" in filtered_specs

    # Provider grouping sorts active provider first
    groups = {}
    for spec, name, prov in screen._filtered_models:
        groups.setdefault(prov, []).append((spec, name, prov))

    def _sort_key(prov: str):
        from talkops.model.config import (
            DEFAULT_PROVIDER_PRIORITY,
            get_provider_auth_status,
        )
        if prov == "dynamic":
            return (0, 0, prov)
        if screen._current_provider and prov == screen._current_provider:
            return (1, 0, prov)
        auth = get_provider_auth_status(prov)
        is_authed = auth.as_legacy_bool() is True
        tier = 2 if is_authed else 3
        p_idx = DEFAULT_PROVIDER_PRIORITY.index(prov) if prov in DEFAULT_PROVIDER_PRIORITY else 99
        return (tier, p_idx, prov)

    sorted_provs = sorted(groups.keys(), key=_sort_key)
    assert sorted_provs[0] == "dynamic"
    assert sorted_provs[1] == "google_genai"


def test_pool_selector_and_model_selector_model_consistency():
    """Verify pool selector and model selector model consistency."""
    from talkops.model.config import get_curated_models_for_provider
    from talkops.ui.widgets.pool_selector import PoolSelectorScreen

    ps = PoolSelectorScreen(active_provider="google_genai")
    pool_options = ps._get_model_options_for_provider("google_genai")
    curated = get_curated_models_for_provider("google_genai")

    assert len(pool_options) == len(curated)
    for (disp, spec), (cur_id, cur_disp) in zip(pool_options, curated):
        assert spec == f"google_genai:{cur_id}"


def test_model_option_hover_and_in_place_selection():
    """Verify model option hover and in place selection."""
    from talkops.ui.widgets.model_selector import ModelOption

    opt = ModelOption(
        label="  Gemini 3.7 Flash",
        model_spec="google_genai:gemini-3.7-flash",
        provider="google_genai",
        index=3,
        display_name="Gemini 3.7 Flash",
        is_selected=False,
    )
    assert not opt.is_selected
    assert "model-option-selected" not in opt.classes

    # Toggle selected True in-place
    opt.set_selected(True)
    assert opt.is_selected
    assert "model-option-selected" in opt.classes
    assert "› Gemini 3.7 Flash" in opt.render_label_text()

    # Toggle selected False in-place
    opt.set_selected(False)
    assert not opt.is_selected
    assert "model-option-selected" not in opt.classes
    assert "  Gemini 3.7 Flash" in opt.render_label_text()


def test_model_selector_smooth_navigation_and_jev_system1():
    """Verify model selector smooth navigation and jev system1."""
    from talkops.ui.widgets.model_selector import ModelOption, ModelSelectorScreen

    screen = ModelSelectorScreen(
        current_model="gemini-3.5-flash",
        current_provider="google_genai",
    )
    screen.on_mount()

    # Verify Jev entry is clean, accurate, and has NO emojis
    auto_model = next(m for m in screen._all_models if m[0] == "auto")
    assert "⚡" not in auto_model[1]
    assert "TypeSafe Jev System 1" in auto_model[1]

    # Mock option widgets for screen
    opt0 = ModelOption(
        label="› Option 0",
        model_spec="auto",
        provider="dynamic",
        index=0,
        display_name="Option 0",
        is_selected=True,
    )
    opt1 = ModelOption(
        label="  Option 1",
        model_spec="google_genai:gemini-3.7-flash",
        provider="google_genai",
        index=1,
        display_name="Option 1",
        is_selected=False,
    )
    screen._option_widgets = [opt0, opt1]
    screen._flat_order = [("auto", "Option 0", "dynamic"), ("google_genai:gemini-3.7-flash", "Option 1", "google_genai")]
    screen._selected_index = 0

    # Moving down: updates widgets in-place without rebuilding DOM
    screen._move_selection(1)
    assert screen._selected_index == 1
    assert not opt0.is_selected
    assert opt1.is_selected

    # Moving down wraps around to 0 smoothly
    screen._move_selection(1)
    assert screen._selected_index == 0
    assert opt0.is_selected
    assert not opt1.is_selected

    # Moving up from 0 wraps around to 1
    screen._move_selection(-1)
    assert screen._selected_index == 1
    assert not opt0.is_selected
    assert opt1.is_selected


async def test_model_selector_dismiss_idempotency_and_auth_check(monkeypatch: pytest.MonkeyPatch):
    """Verify ModelSelectorScreen dismissal is idempotent and prevents ScreenStackError."""
    from talkops.ui.app import TalkOpsApp
    from talkops.ui.widgets.model_selector import ModelSelectorScreen

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-mock")
    app = TalkOpsApp()
    async with app.run_test(headless=True) as pilot:
        screen = ModelSelectorScreen()
        results = []
        app.push_screen(screen, lambda res: results.append(res))
        await pilot.pause()
        assert len(app._screen_stack) == 2

        # 1st call via _select_with_auth_check
        screen._select_with_auth_check("google_genai:gemini-3.6-flash", "google_genai", None)
        await pilot.pause()
        assert screen._dismissed is True
        assert len(app._screen_stack) == 1
        assert len(results) == 1

        # 2nd call (simulating double click or rapid re-trigger that previously caused ScreenStackError)
        screen._select_with_auth_check("google_genai:gemini-3.6-flash", "google_genai", None)
        await pilot.pause()
        assert len(app._screen_stack) == 1
        assert len(results) == 1


def test_model_option_multi_click_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify ModelOption ignores rapid multi-clicks (chain > 1)."""
    from talkops.ui.widgets.model_selector import ModelOption

    opt = ModelOption(
        label="  Gemini 3.6 Flash",
        model_spec="google_genai:gemini-3.6-flash",
        provider="google_genai",
        index=0,
    )

    posted: list[Message] = []

    def fake_post_message(message: Message) -> bool:
        """Provide fake post message test helper or fixture."""
        posted.append(message)
        return True

    monkeypatch.setattr(opt, "post_message", fake_post_message)

    # First click (chain=1) -> posted
    click1 = Click(opt, x=0, y=0, delta_x=0, delta_y=0, button=1, shift=False, meta=False, ctrl=False, chain=1)
    opt.on_click(click1)
    assert len(posted) == 1
    assert isinstance(posted[0], ModelOption.Clicked)
    assert posted[0].model_spec == "google_genai:gemini-3.6-flash"

    # Second click of a double-click (chain=2) -> suppressed!
    click2 = Click(opt, x=0, y=0, delta_x=0, delta_y=0, button=1, shift=False, meta=False, ctrl=False, chain=2)
    opt.on_click(click2)
    assert len(posted) == 1


async def test_app_pop_screen_guards_against_screen_stack_error():
    """Verify TalkOpsApp.pop_screen does not raise ScreenStackError when stack size <= 1."""
    from talkops.ui.app import TalkOpsApp

    app = TalkOpsApp()
    async with app.run_test(headless=True):
        assert len(app._screen_stack) == 1
        # Should not raise ScreenStackError!
        res = app.pop_screen()
        assert res is not None


def test_gemini_3_8_flash_spec_and_reasoning_profiles():
    """Verify Gemini 3.8 Flash registration, capabilities, and reasoning effort across providers."""
    from talkops.model.config import get_model_profile
    from talkops.model.reasoning import (
        default_effort_for_model,
        is_effort_supported_for_model,
        supported_efforts_for_model,
    )

    gemini_3_8_specs = [
        "google_genai:gemini-3.8-flash",
        "google_vertexai:gemini-3.8-flash",
        "openrouter:google/gemini-3.8-flash",
        "litellm:gemini/gemini-3.8-flash",
    ]

    for spec in gemini_3_8_specs:
        entry = get_model_profile(spec)
        assert entry is not None, f"Profile missing for {spec}"
        profile = entry.get("profile", {})
        assert profile.get("max_input_tokens") == 1_000_000, f"Max input tokens mismatch for {spec}"
        assert profile.get("max_output_tokens") == 64_000, f"Max output tokens mismatch for {spec}"
        assert profile.get("tool_calling") is True, f"Tool calling should be enabled for {spec}"
        assert profile.get("structured_output") is True, f"Structured output should be enabled for {spec}"
        assert profile.get("reasoning_output") is True, f"Reasoning output should be enabled for {spec}"
        assert profile.get("reasoning_effort_levels") == ["low", "medium", "high"], f"Reasoning levels mismatch for {spec}"
        assert profile.get("reasoning_effort_default") == "medium", f"Default reasoning mismatch for {spec}"

        # Reasoning effort helpers
        assert is_effort_supported_for_model(spec, "low") is True
        assert is_effort_supported_for_model(spec, "medium") is True
        assert is_effort_supported_for_model(spec, "high") is True
        assert default_effort_for_model(spec) == "medium"
        assert set(supported_efforts_for_model(spec)) == {"low", "medium", "high"}


def test_claude_on_google_vertexai_raises_actionable_error():
    """Requesting a Claude model under google_vertexai instructs the user to use google_anthropic_vertex."""
    clear_model_cache()
    with pytest.raises(ModelConfigError) as exc_info:
        create_model("google_vertexai:claude-3-5-sonnet-v2@20241022")
    assert "google_anthropic_vertex" in str(exc_info.value)
    assert "google_vertexai" in str(exc_info.value)


async def test_model_selector_batch_mount_navigation_and_fuzzy_search():
    """Verify batch mount, wrap-around keyboard navigation, fuzzy search, and page navigation."""
    from talkops.ui.app import TalkOpsApp
    from talkops.ui.widgets.model_selector import ModelSelectorScreen

    app = TalkOpsApp()
    async with app.run_test(headless=True) as pilot:
        screen = ModelSelectorScreen()
        app.push_screen(screen)
        await pilot.pause()

        # Input should be focused immediately
        inp = screen.query_one("#model-filter", Input)
        assert inp.has_focus

        # Verify models are loaded and options mounted in batch
        assert screen._loaded is True
        assert len(screen._option_widgets) > 0
        total_widgets = len(screen._option_widgets)

        # Initial selection should be at index 0
        assert screen._selected_index == 0

        # Up arrow from 0 should wrap around to the last item
        screen.action_move_up()
        assert screen._selected_index == total_widgets - 1

        # Down arrow from last item should wrap around to 0
        screen.action_move_down()
        assert screen._selected_index == 0

        # Page down moves by page
        screen.action_page_down()
        assert screen._selected_index > 0

        # Page up moves back towards 0
        screen.action_page_up()
        assert screen._selected_index == 0

        # Test fuzzy filtering
        inp.value = "gemini 3.8"
        await pilot.pause()
        filtered_specs = [opt.model_spec for opt in screen._option_widgets]
        assert any("gemini-3.8-flash" in s for s in filtered_specs)

        # Tab completion
        screen.action_tab_complete()
        assert inp.value == screen._option_widgets[screen._selected_index].model_spec

        # Resize refit
        screen._fit_model_list()


def test_provider_aliases_canonicalization_and_storage(monkeypatch, tmp_path):
    """Verify provider aliases canonicalize across config, spec resolution, and storage."""
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.model.config import (
        ProviderAuthState,
        canonicalize_provider,
        get_base_url_env_var,
        get_credential_env_var,
        get_provider_auth_status,
        normalize_model_spec,
        resolve_model_spec,
    )

    assert canonicalize_provider("google") == "google_genai"
    assert canonicalize_provider("gemini") == "google_genai"
    assert canonicalize_provider("vertexai") == "google_vertexai"
    assert canonicalize_provider("vertex") == "google_vertexai"
    assert canonicalize_provider("mistral") == "mistralai"
    assert canonicalize_provider("azure") == "azure_openai"
    assert canonicalize_provider("watsonx") == "ibm"

    assert get_credential_env_var("google") == "GOOGLE_API_KEY"
    assert get_credential_env_var("mistral") == "MISTRAL_API_KEY"
    assert get_base_url_env_var("azure_openai") == "AZURE_OPENAI_ENDPOINT"
    assert get_base_url_env_var("ibm") == "WATSONX_URL"
    assert get_base_url_env_var("ollama") == "OLLAMA_HOST"

    assert resolve_model_spec("vertexai:gemini-2.5-pro") == ("google_vertexai", "gemini-2.5-pro")
    assert normalize_model_spec("mistral:mistral-large-latest") == "mistralai:mistral-large-latest"

    storage = CompositeCredentialStorage(
        auth_file_path=tmp_path / "auth.json",
        env_file_path=tmp_path / ".env",
    )
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_BASE_URL", raising=False)

    assert storage.set_provider_key(
        "google",
        "AIza-test-123456",
        base_url="https://custom.gemini.internal",
        sync_dotenv=True,
    )
    assert storage.get_provider_key("google_genai") == "AIza-test-123456"
    assert storage.get_provider_key("google") == "AIza-test-123456"
    status = get_provider_auth_status("google_genai")
    assert status.state == ProviderAuthState.CONFIGURED


def test_custom_providers_from_config_toml(monkeypatch, tmp_path):
    """Verify [models.providers.<name>] in config.toml loads custom class_path, kwargs, and profile."""
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from talkops.config import paths
    from talkops.model.config import (
        ModelConfig,
        ProviderAuthState,
        get_base_url_env_vars,
        get_credential_env_var,
        get_provider_auth_status,
    )
    from talkops.model.factory import clear_model_cache, create_model

    class DummyCustomChatModel(BaseChatModel):
        model_name: str = ""
        base_url: str | None = None
        api_key: str | None = None
        custom_param: str | None = None

        def __init__(self, model: str = "", **kwargs):
            super().__init__(
                model_name=model,
                base_url=kwargs.get("base_url"),
                api_key=kwargs.get("api_key"),
                custom_param=kwargs.get("custom_param"),
            )

        @property
        def _llm_type(self) -> str:
            return "dummy_custom"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    import sys
    monkeypatch.setitem(sys.modules, "dummy_custom_provider_mod", MagicMock(DummyCustomChatModel=DummyCustomChatModel))

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[models.providers.custom_vllm]
class_path = "dummy_custom_provider_mod:DummyCustomChatModel"
base_url_env = "CUSTOM_VLLM_URL"
base_url = "http://vllm.internal:8000/v1"
api_key_env = "CUSTOM_VLLM_KEY"
models = ["qwen-coder"]
profile = { max_input_tokens = 65536 }

[models.providers.custom_vllm.params]
custom_param = "global_val"

[models.providers.custom_vllm.params."qwen-coder"]
custom_param = "per_model_val"

[models.providers.local_llama_cpp]
class_path = "dummy_custom_provider_mod:DummyCustomChatModel"
base_url = "http://127.0.0.1:8080/v1"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr("talkops.config.toml_config.CONFIG_PATH", cfg_file)
    clear_model_cache()

    cfg = ModelConfig.load()
    assert "custom_vllm" in cfg.providers
    assert get_credential_env_var("custom_vllm") == "CUSTOM_VLLM_KEY"
    assert "CUSTOM_VLLM_URL" in get_base_url_env_vars("custom_vllm")

    # Local class_path provider without api_key_env does not require credentials
    local_status = get_provider_auth_status("local_llama_cpp")
    assert local_status.state == ProviderAuthState.NOT_REQUIRED

    # custom_vllm requires CUSTOM_VLLM_KEY
    monkeypatch.delenv("CUSTOM_VLLM_KEY", raising=False)
    assert get_provider_auth_status("custom_vllm").state == ProviderAuthState.MISSING
    monkeypatch.setenv("CUSTOM_VLLM_KEY", "vllm-secret-token")
    assert get_provider_auth_status("custom_vllm").state == ProviderAuthState.CONFIGURED

    res = create_model("custom_vllm:qwen-coder")
    assert res.provider == "custom_vllm"
    assert res.model_name == "qwen-coder"
    assert res.context_limit == 65536
    assert getattr(res.model, "base_url", None) == "http://vllm.internal:8000/v1"
    assert getattr(res.model, "api_key", None) == "vllm-secret-token"
    assert getattr(res.model, "custom_param", None) == "per_model_val"
    clear_model_cache()


def test_provider_specific_kwargs_and_base_url_mapping(monkeypatch, tmp_path):
    """Verify Cohere, Perplexity, IBM watsonx, Bedrock, LiteLLM, Vertex AI, and Ollama kwargs."""
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.model.config import ModelConfig, normalize_ollama_endpoint
    from talkops.model.factory import _get_provider_kwargs, clear_model_cache, create_model

    storage = CompositeCredentialStorage(
        auth_file_path=tmp_path / "auth.json",
        env_file_path=tmp_path / ".env",
    )
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)

    # 1. Cohere: cohere_api_key without duplicate api_key
    monkeypatch.setenv("COHERE_API_KEY", "cohere-test-key")
    cohere_kwargs = _get_provider_kwargs("cohere", model_name="command-a-03-2025")
    assert cohere_kwargs.get("cohere_api_key") == "cohere-test-key"
    assert "api_key" not in cohere_kwargs
    clear_model_cache()
    cohere_res = create_model("cohere:command-a-03-2025")
    assert cohere_res.provider == "cohere"

    # 2. Perplexity: pplx_api_key + PERPLEXITY_BASE_URL without api_key or base_url in kwargs
    monkeypatch.setenv("PPLX_API_KEY", "pplx-test-key")
    monkeypatch.setenv("PERPLEXITY_BASE_URL", "https://pplx.proxy.internal")
    pplx_kwargs = _get_provider_kwargs("perplexity", model_name="sonar-pro")
    assert pplx_kwargs.get("pplx_api_key") == "pplx-test-key"
    assert "api_key" not in pplx_kwargs
    assert "base_url" not in pplx_kwargs
    clear_model_cache()
    pplx_res = create_model("perplexity:sonar-pro")
    assert pplx_res.provider == "perplexity"

    # 3. IBM watsonx: apikey + url + project_id without api_key or base_url
    monkeypatch.setenv("WATSONX_APIKEY", "wx-test-key")
    monkeypatch.setenv("WATSONX_URL", "https://eu-de.ml.cloud.ibm.com")
    monkeypatch.setenv("WATSONX_PROJECT_ID", "proj-123")
    ibm_kwargs = _get_provider_kwargs("ibm", model_name="ibm/granite-3-8b-instruct")
    assert ibm_kwargs.get("apikey") == "wx-test-key"
    assert ibm_kwargs.get("url") == "https://eu-de.ml.cloud.ibm.com"
    assert ibm_kwargs.get("project_id") == "proj-123"
    assert "api_key" not in ibm_kwargs
    assert "base_url" not in ibm_kwargs

    # 4. Bedrock: endpoint_url without base_url
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_TEST")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "https://vpce-bedrock.us-east-1.vpce.amazonaws.com")
    bedrock_kwargs = _get_provider_kwargs("bedrock", model_name="us.anthropic.claude-sonnet-4-6-v1:0")
    assert bedrock_kwargs.get("endpoint_url") == "https://vpce-bedrock.us-east-1.vpce.amazonaws.com"
    assert "base_url" not in bedrock_kwargs

    # 5. LiteLLM: api_base without base_url
    monkeypatch.setenv("LITELLM_API_KEY", "litellm-test-key")
    monkeypatch.setenv("LITELLM_API_BASE", "http://litellm.internal:4000")
    litellm_kwargs = _get_provider_kwargs("litellm", model_name="gpt-4o")
    assert litellm_kwargs.get("api_base") == "http://litellm.internal:4000"
    assert "base_url" not in litellm_kwargs

    # 6. Vertex AI: api_endpoint + GCP_PROJECT_ID + GCP_LOCATION
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "my-gcp-proj")
    monkeypatch.setenv("GCP_LOCATION", "europe-west4")
    monkeypatch.setenv("VERTEXAI_API_ENDPOINT", "europe-west4-aiplatform.googleapis.com")
    vertex_kwargs = _get_provider_kwargs("vertexai", model_name="gemini-2.5-pro")
    assert vertex_kwargs.get("project") == "my-gcp-proj"
    assert vertex_kwargs.get("location") == "europe-west4"
    assert vertex_kwargs.get("api_endpoint") == "europe-west4-aiplatform.googleapis.com"
    assert "base_url" not in vertex_kwargs

    # 7. Ollama: OLLAMA_HOST normalization
    assert normalize_ollama_endpoint("127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert normalize_ollama_endpoint("0.0.0.0:11434") == "http://localhost:11434"
    assert normalize_ollama_endpoint("https://ollama.internal:11434/") == "https://ollama.internal:11434"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert ModelConfig.load().get_base_url("ollama") == "http://localhost:11434"


def test_cerebras_and_sambanova_openai_compatible_routing(monkeypatch, tmp_path):
    """Verify cerebras and sambanova resolve default base URLs and route through OpenAI-compatible init."""
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.model.factory import _get_provider_kwargs, clear_model_cache, create_model

    storage = CompositeCredentialStorage(
        auth_file_path=tmp_path / "auth.json",
        env_file_path=tmp_path / ".env",
    )
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)

    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test-key")
    monkeypatch.setenv("SAMBANOVA_API_KEY", "snova-test-key")

    cb_kwargs = _get_provider_kwargs("cerebras", model_name="llama-3.3-70b")
    assert cb_kwargs.get("base_url") == "https://api.cerebras.ai/v1"
    assert cb_kwargs.get("api_key") == "csk-test-key"

    sn_kwargs = _get_provider_kwargs("sambanova", model_name="Meta-Llama-3.3-70B-Instruct")
    assert sn_kwargs.get("base_url") == "https://api.sambanova.ai/v1"
    assert sn_kwargs.get("api_key") == "snova-test-key"

    clear_model_cache()
    cb_res = create_model("cerebras:llama-3.3-70b")
    assert cb_res.provider == "cerebras"
    sn_res = create_model("sambanova:Meta-Llama-3.3-70B-Instruct")
    assert sn_res.provider == "sambanova"
    clear_model_cache()


def test_fireworks_and_vertexai_protobuf_coexistence():
    """Verify langchain_fireworks and langchain_google_vertexai coexist without duplicate proto errors."""
    import langchain_fireworks
    from langchain_google_vertexai import ChatVertexAI
    from langchain_google_vertexai.model_garden import ChatAnthropicVertex

    assert langchain_fireworks is not None
    assert ChatVertexAI is not None
    assert ChatAnthropicVertex is not None


def test_vertex_claude_models_under_google_anthropic_vertex_and_package_names():
    """Verify Claude models are under google_anthropic_vertex and package names resolve accurately."""
    from talkops.model.config import AVAILABLE_MODELS, provider_package_name

    vertex_models = [m[0] for m in AVAILABLE_MODELS.get("google_vertexai", [])]
    assert not any(m.startswith("claude-") for m in vertex_models)

    anth_vertex_models = [m[0] for m in AVAILABLE_MODELS.get("google_anthropic_vertex", [])]
    assert any(m.startswith("claude-") for m in anth_vertex_models)

    assert provider_package_name("bedrock") == "langchain-aws"
    assert provider_package_name("vertex") == "langchain-google-vertexai"
    assert provider_package_name("google_vertexai") == "langchain-google-vertexai"
    assert provider_package_name("nvidia") == "langchain-nvidia-ai-endpoints"
    assert provider_package_name("cerebras") == "langchain-openai"
    assert provider_package_name("sambanova") == "langchain-openai"


def test_ollama_cross_provider_message_content_compatibility():
    """Verify ChatOllama and sanitize_messages_for_model handle cross-provider thinking/reasoning blocks."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from talkops.middleware.unified_system_message import sanitize_messages_for_model
    from talkops.model.factory import clear_model_cache, create_model

    clear_model_cache()
    ollama_res = create_model("ollama:qwen3.8:latest")
    ollama_model = ollama_res.model

    gemini_ai = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Processing user greeting", "index": 0},
            {"type": "text", "text": "Hello from Gemini", "index": 0, "extras": {"signature": "abc"}},
        ],
        response_metadata={"model_name": "gemini-3.5-flash", "model_provider": "google_genai"},
    )
    anthropic_ai = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Claude thought", "signature": "sig123"},
            {"type": "redacted_thinking", "data": "enc123"},
            {"type": "text", "text": "Hello from Claude"},
        ],
        response_metadata={"model_name": "claude-sonnet-4-6", "model_provider": "anthropic"},
    )
    openai_ai = AIMessage(
        content=[
            {"type": "reasoning", "id": "rs_123", "summary": [{"type": "summary_text", "text": "OpenAI thought"}]},
            {"type": "text", "text": "Hello from OpenAI", "annotations": []},
        ],
        response_metadata={"model_name": "gpt-5.4", "model_provider": "openai"},
    )

    msgs = [
        SystemMessage(content=[{"type": "text", "text": "You are TalkOps."}]),
        HumanMessage(content="who are you"),
        gemini_ai,
        HumanMessage(content="And Claude?"),
        anthropic_ai,
        HumanMessage(content="And OpenAI?"),
        openai_ai,
        HumanMessage(content="hi"),
    ]

    # 1. Direct ChatOllama._convert_messages_to_ollama_messages must not raise ValueError
    ollama_msgs = ollama_model._convert_messages_to_ollama_messages(msgs)
    assert len(ollama_msgs) == 8
    assert "Hello from Gemini" in ollama_msgs[2]["content"]
    assert "Hello from Claude" in ollama_msgs[4]["content"]
    assert "Hello from OpenAI" in ollama_msgs[6]["content"]

    # 2. Middleware sanitize_messages_for_model collapses cross-provider blocks cleanly
    sanitized = sanitize_messages_for_model(msgs, ollama_model)
    assert sanitized is not None
    assert isinstance(sanitized[0].content, str)
    assert sanitized[2].content == "Hello from Gemini"
    assert sanitized[4].content == "Hello from Claude"
    assert sanitized[6].content == "Hello from OpenAI"


def test_ollama_available_models_list_only_shows_installed(monkeypatch):
    """Verify get_available_models_list only shows installed Ollama models, not uninstalled static catalog entries."""
    from talkops.model.config import get_available_models_list

    monkeypatch.setattr(
        "talkops.model.config._fetch_ollama_installed_models",
        lambda _endpoint=None: ["llama3.2:latest", "qwen3.8:latest"],
    )
    avail = get_available_models_list()
    ollama_entries = [m for m in avail if m[2] == "ollama"]
    ollama_specs = [m[0] for m in ollama_entries]
    assert ollama_specs == ["ollama:llama3.2:latest", "ollama:qwen3.8:latest"]
    assert all("(installed)" in m[1] for m in ollama_entries)


def test_gemini_4_argon_model_catalog_and_profiles():
    """Verify Gemini 4 Argon is registered across Gemini providers with reasoning support."""
    from talkops.model.config import AVAILABLE_MODELS, get_model_profile
    from talkops.model.pool import supported_efforts_for_model

    genai_ids = [m[0] for m in AVAILABLE_MODELS["google_genai"]]
    vertex_ids = [m[0] for m in AVAILABLE_MODELS["google_vertexai"]]
    openrouter_ids = [m[0] for m in AVAILABLE_MODELS["openrouter"]]
    litellm_ids = [m[0] for m in AVAILABLE_MODELS["litellm"]]

    assert "gemini-4-argon" in genai_ids
    assert "gemini-4-argon" in vertex_ids
    assert "google/gemini-4-argon" in openrouter_ids
    assert "gemini/gemini-4-argon" in litellm_ids

    for spec in (
        "google_genai:gemini-4-argon",
        "google_vertexai:gemini-4-argon",
        "openrouter:google/gemini-4-argon",
        "litellm:gemini/gemini-4-argon",
    ):
        prof = get_model_profile(spec)
        assert prof is not None
        assert prof["profile"]["max_input_tokens"] == 1048576
        assert prof["profile"]["max_output_tokens"] == 65536
        assert supported_efforts_for_model(spec) == ("low", "medium", "high")


def test_vertex_and_ambient_bedrock_not_falsely_activated(monkeypatch):
    """Verify Vertex AI, Anthropic Vertex, and ambient AWS CLI credentials do not falsely activate providers."""
    import os
    from talkops.config.settings import get_settings
    from talkops.model.config import (
        ProviderAuthState,
        apply_stored_credentials,
        get_provider_auth_status,
    )

    s = get_settings()
    for k in (
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT_ID",
        "VERTEXAI_PROJECT",
        "VERTEXAI_API_KEY",
        "GOOGLE_VERTEXAI_API_KEY",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "TALKOPS_AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "BEDROCK_API_KEY",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
    ):
        monkeypatch.delenv(k, raising=False)

    monkeypatch.setattr(s, "google_cloud_project", None, raising=False)
    monkeypatch.setattr(s, "google_api_key", None, raising=False)
    monkeypatch.setattr(s, "google_genai_use_vertexai", False, raising=False)
    monkeypatch.setattr(s, "model_name", None, raising=False)
    monkeypatch.setattr(s, "model", None, raising=False)

    # 1. Vertex providers must be MISSING when unconfigured (not IMPLICIT)
    vtx_status = get_provider_auth_status("google_vertexai")
    anth_vtx_status = get_provider_auth_status("google_anthropic_vertex")
    assert vtx_status.state == ProviderAuthState.MISSING
    assert anth_vtx_status.state == ProviderAuthState.MISSING
    assert vtx_status.as_legacy_bool() is False
    assert anth_vtx_status.as_legacy_bool() is False

    # 2. Unconfigured apply_stored_credentials("google_vertexai") must not pollute google_genai
    apply_stored_credentials("google_vertexai")
    assert os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") != "true"
    assert get_provider_auth_status("google_genai").state == ProviderAuthState.MISSING

    # 3. Setting GOOGLE_API_KEY (AI Studio) activates google_genai, NOT google_vertexai / google_anthropic_vertex
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-studio-key")
    assert get_provider_auth_status("google_genai").state == ProviderAuthState.CONFIGURED
    assert get_provider_auth_status("google_vertexai").state == ProviderAuthState.MISSING
    assert get_provider_auth_status("google_anthropic_vertex").state == ProviderAuthState.MISSING

    # 4. Ambient shell AWS credentials matching _INITIAL_SHELL_AWS_CREDS do not activate bedrock
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_AMBIENT_SHELL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET_AMBIENT_SHELL")
    monkeypatch.setattr(
        "talkops.model.config._INITIAL_SHELL_AWS_CREDS",
        {"AWS_ACCESS_KEY_ID": "AKIA_AMBIENT_SHELL", "AWS_SECRET_ACCESS_KEY": "SECRET_AMBIENT_SHELL"},
    )
    monkeypatch.setattr("talkops.config.toml_config.load_default_model", lambda: None)
    monkeypatch.setattr("talkops.config.toml_config.load_recent_model", lambda: None)
    assert get_provider_auth_status("bedrock").state == ProviderAuthState.MISSING


def test_vertex_ai_api_key_does_not_activate_google_genai_or_pass_key_as_gcp_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Configuring Google Vertex AI with an AQ.* API key does not mark Google Gemini as configured or set project=AQ.*."""
    import os
    from talkops.auth.storage import CompositeCredentialStorage
    from talkops.config.settings import get_settings
    from talkops.model.config import (
        ProviderAuthState,
        apply_stored_credentials,
        get_provider_auth_status,
    )
    from talkops.model.factory import _get_provider_kwargs, clear_model_cache, create_model

    storage = CompositeCredentialStorage(
        auth_file_path=tmp_path / "auth.json",
        env_file_path=tmp_path / ".env",
    )
    monkeypatch.setattr("talkops.auth.storage.get_credential_storage", lambda: storage)
    monkeypatch.setattr("talkops.state.remote_session._can_use_disk_auth_session", lambda: True)

    s = get_settings()
    for k in (
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT_ID",
        "VERTEXAI_PROJECT",
        "VERTEXAI_API_KEY",
        "GOOGLE_VERTEXAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(s, "google_cloud_project", None, raising=False)
    monkeypatch.setattr(s, "google_api_key", None, raising=False)
    monkeypatch.setattr(s, "google_genai_use_vertexai", False, raising=False)

    vertex_key = "AQ.test-dummy-vertex-express-key"
    storage.set_provider_key("google_vertexai", vertex_key, env_var="GOOGLE_CLOUD_PROJECT")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", vertex_key)

    # 1. google_vertexai is CONFIGURED, while google_genai remains MISSING
    assert get_provider_auth_status("google_vertexai").state == ProviderAuthState.CONFIGURED
    assert get_provider_auth_status("google_genai").state == ProviderAuthState.MISSING

    # 2. apply_stored_credentials("google_vertexai") moves AQ.* key out of GOOGLE_CLOUD_PROJECT into VERTEXAI_API_KEY
    apply_stored_credentials("google_vertexai")
    assert os.environ.get("VERTEXAI_API_KEY") == vertex_key
    assert os.environ.get("GOOGLE_CLOUD_PROJECT") is None
    assert os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") != "true"
    assert get_provider_auth_status("google_genai").state == ProviderAuthState.MISSING

    # 3. _get_provider_kwargs("google_vertexai") sets vertexai=True and google_api_key=AQ.*, never project=AQ.*
    vtx_kwargs = _get_provider_kwargs("google_vertexai", model_name="gemini-4-argon")
    assert vtx_kwargs.get("vertexai") is True
    assert vtx_kwargs.get("google_api_key") == vertex_key
    assert "project" not in vtx_kwargs
    assert "location" not in vtx_kwargs

    # 4. create_model("google_vertexai:gemini-4-argon") builds a ChatGoogleGenerativeAI client in Vertex Express Mode
    clear_model_cache()
    res = create_model("google_vertexai:gemini-4-argon")
    assert res.provider == "google_vertexai"
    assert res.model_name == "gemini-4-argon"
    assert getattr(res.model, "vertexai", None) is True
    assert getattr(res.model, "project", None) is None
    assert res.model.client._api_client.api_key == vertex_key
    assert res.model.client._api_client.project is None
    clear_model_cache()


def test_vertex_express_mode_global_project_routing_and_404_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Vertex AI Express Mode sets project/location=global when resolved and rewrites bare 404 URLs."""
    from talkops.model.factory import (
        _VERTEX_EXPRESS_PROJECT_CACHE,
        clear_model_cache,
        create_model,
    )

    vertex_key = "AQ.test-dummy-vertex-express-key"
    monkeypatch.setenv("VERTEXAI_API_KEY", vertex_key)
    monkeypatch.setenv("TALKOPS_VERTEX_EXPRESS_PROJECT", "492468289179")
    _VERTEX_EXPRESS_PROJECT_CACHE.clear()
    clear_model_cache()

    res = create_model("google_vertexai:gemini-4-argon")
    api_client = res.model.client._api_client
    assert api_client.api_key == vertex_key
    assert api_client.project == "492468289179"
    assert api_client.location == "global"

    req = api_client._build_request(
        "post",
        "publishers/google/models/gemini-4-argon:streamGenerateContent",
        {"contents": []},
    )
    assert "projects/492468289179/locations/global/publishers/google/models/gemini-4-argon:streamGenerateContent" in req.url
    assert req.headers.get("x-goog-api-key") == vertex_key

    _VERTEX_EXPRESS_PROJECT_CACHE.clear()
    clear_model_cache()


@pytest.mark.asyncio
async def test_vertex_express_patch_forwards_implicit_retry_and_extra_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify patched _request_once and _async_request_once forward 'implicit_retry' and arbitrary kwargs."""
    import logging
    from types import SimpleNamespace
    from typing import Any
    from google.genai import _api_client as genai_api_client
    from google.genai import errors as genai_errors
    import talkops.model.factory as factory_mod

    monkeypatch.setattr(factory_mod, "_VERTEX_EXPRESS_CLIENT_PATCHED", False)

    sync_calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    async_calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def fake_orig_sync(self: Any, http_request: Any, *args: Any, **kwargs: Any) -> str:
        sync_calls.append((http_request.url, args, kwargs))
        if len(sync_calls) == 1:
            err = genai_errors.ClientError(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": "Publisher Model `projects/492468289179/locations/asia-southeast1/publishers/google/models/gemini-4-argon` was not found",
                    }
                },
            )
            raise err
        return "sync-ok"

    async def fake_orig_async(self: Any, http_request: Any, *args: Any, **kwargs: Any) -> str:
        async_calls.append((http_request.url, args, kwargs))
        if len(async_calls) == 1:
            err = genai_errors.ClientError(
                404,
                {
                    "error": {
                        "code": 404,
                        "message": "Publisher Model `projects/492468289179/locations/asia-southeast1/publishers/google/models/gemini-4-argon` was not found",
                    }
                },
            )
            raise err
        return "async-ok"

    monkeypatch.setattr(genai_api_client.BaseApiClient, "_request_once", fake_orig_sync)
    monkeypatch.setattr(genai_api_client.BaseApiClient, "_async_request_once", fake_orig_async)

    factory_mod._ensure_vertex_express_api_client_patch()

    dummy_client = SimpleNamespace(vertexai=True, api_key="AQ.test-key", project=None, location=None)
    sync_req = SimpleNamespace(url="https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-4-argon:generateContent")
    sync_res = genai_api_client.BaseApiClient._request_once(
        dummy_client,
        sync_req,
        True,
        implicit_retry=True,
    )
    assert sync_res == "sync-ok"
    assert len(sync_calls) == 2
    assert sync_calls[0][1] == (True,)
    assert sync_calls[0][2] == {"implicit_retry": True}
    assert sync_calls[1][1] == (True,)
    assert sync_calls[1][2] == {"implicit_retry": True}
    assert "/projects/492468289179/locations/global/publishers/" in sync_calls[1][0]

    dummy_async_client = SimpleNamespace(vertexai=True, api_key="AQ.test-key", project=None, location=None)
    async_req = SimpleNamespace(url="https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-4-argon:streamGenerateContent")
    async_res = await genai_api_client.BaseApiClient._async_request_once(
        dummy_async_client,
        async_req,
        stream=True,
        implicit_retry=False,
    )
    assert async_res == "async-ok"
    assert len(async_calls) == 2
    assert async_calls[0][2] == {"stream": True, "implicit_retry": False}
    assert async_calls[1][2] == {"stream": True, "implicit_retry": False}
    assert "/projects/492468289179/locations/global/publishers/" in async_calls[1][0]

    # Verify probe log filter suppresses gemini-2.5-flash-probe 404 logs
    flt = factory_mod._VertexProbeLogFilter()
    probe_rec = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='HTTP Request: POST https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-2.5-flash-probe:countTokens "HTTP/1.1 404 Not Found"',
        args=(),
        exc_info=None,
    )
    normal_rec = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='HTTP Request: POST https://aiplatform.googleapis.com/v1beta1/publishers/google/models/gemini-4-argon:generateContent "HTTP/1.1 200 OK"',
        args=(),
        exc_info=None,
    )
    assert flt.filter(probe_rec) is False
    assert flt.filter(normal_rec) is True








