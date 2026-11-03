"""Boot-time assembly of the hook registry.

Lifted out of ``chief.app`` so ``build_app`` stays a table of contents under
the 200-line cap. The cwd-relative discovery constants are imported here now,
so this module — not ``chief.app`` — is what tests patch to isolate a boot
from the operator's install state (see ``tests/conftest.py``).
"""

from pathlib import Path

from chief.audit import AuditLog
from chief.budget import Budget
from chief.classifiers import Classifier, ClassifierRegistry
from chief.config import Config, load_raw
from chief.hooks.loader import load_hooks
from chief.hooks.registry import HookRegistry
from chief.install.updatecheck import session_start_notice
from chief.pkg import CLONED_PACKAGES_DIR, PackageLibrary
from chief.provider.base import Provider
from chief.registry_apply import INSTALLED_REGISTRY, load_installed
from chief.screen import Screen, tool_screen_hook
from chief.typesafe import TypeSafeClient

# The core tool screen bounds itself to this share of the hook runner's
# timeout: a runner timeout passes the result silently, the screen's own
# timeout earns the UNSCREENED banner.
SCREEN_TIMEOUT_SHARE = 0.8


def build_hooks(
    config: Config, provider: Provider, budget: Budget, audit: AuditLog | None = None
) -> tuple[HookRegistry, Classifier, Screen]:
    """Every installed package's hooks, plus the classifier and the screen.

    The classifier is built first and returned because hooks *and* monitors
    share it; the screen rides on it (TypeSafe first, ``screen_fallback``
    model second) and is registered as the core ``post_tool`` hook.
    """
    hooks = HookRegistry()
    typesafe = TypeSafeClient(
        config.typesafe_api_key, budget=budget, base_url=config.typesafe_base_url,
        model=config.typesafe_model, usd_per_mtok=config.typesafe_usd_per_mtok,
        timeout_s=config.typesafe_timeout_s,
    ) if config.typesafe_api_key else None
    classifier_model = config.models.get("default_classifier", config.default_model)
    classifier = Classifier(
        provider, ClassifierRegistry(config.classifiers_dir), classifier_model,
        typesafe=typesafe,
    )
    screen = Screen(
        classifier, threshold=config.typesafe_screen_threshold,
        fallback_model=config.models.get("screen_fallback", classifier_model),
        audit=audit,
    )
    load_hooks(
        library=PackageLibrary((config.packages_dir, CLONED_PACKAGES_DIR)),
        installed=load_installed(INSTALLED_REGISTRY),
        registry=hooks,
        provider=provider,
        models=config.models,
        budget=budget,
        raw_config=load_raw(),
        disabled=config.hooks_disabled,
        data_root=Path("data/hooks"),
        classifier=classifier,
    )
    # Core registers under a package name like anyone else; accessors sort by it.
    hooks.register_session_start("core", session_start_notice(Path.cwd()))
    hooks.register_post_tool("core", tool_screen_hook(
        screen, config.hooks_timeout_seconds * SCREEN_TIMEOUT_SHARE))
    return hooks, classifier, screen
