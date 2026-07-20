"""Boot-time assembly of the hook registry.

Lifted out of ``chief.app`` so ``build_app`` stays a table of contents under
the 200-line cap. The cwd-relative discovery constants are imported here now,
so this module — not ``chief.app`` — is what tests patch to isolate a boot
from the operator's install state (see ``tests/conftest.py``).
"""

from pathlib import Path

from chief.budget import Budget
from chief.classifiers import Classifier, ClassifierRegistry
from chief.config import Config, load_raw
from chief.hooks.loader import load_hooks
from chief.hooks.registry import HookRegistry
from chief.install.updatecheck import session_start_notice
from chief.packages import CLONED_PACKAGES_DIR, PackageLibrary
from chief.provider.base import Provider
from chief.registry_apply import INSTALLED_REGISTRY, load_installed


def build_hooks(
    config: Config, provider: Provider, budget: Budget
) -> tuple[HookRegistry, Classifier]:
    """Every installed package's hooks, plus the classifier primitive.

    The classifier is built first and returned because hooks *and* monitors
    share it. Built before the session manager: hooks get this too.
    """
    hooks = HookRegistry()
    classifier = Classifier(
        provider, ClassifierRegistry(config.classifiers_dir),
        config.models.get("default_classifier", config.default_model))
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
    return hooks, classifier
