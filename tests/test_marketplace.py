"""Real HA Marketplace transactions on local payloads; no network or devices."""
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from awesomeversion import AwesomeVersion
import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.marketplace.base import (
    MarketplaceCommon, MarketplaceSystem, Repositories,
)
from homeassistant.components.marketplace.enums import RepositoryCategory
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.base import RepositoryManifest
from homeassistant.components.marketplace.repositories.integration import IntegrationRepository
from homeassistant.components.marketplace.utils.backup import Backup, restore_interrupted_backups
from homeassistant.components.marketplace.utils.data import MarketplaceData
from homeassistant.components.marketplace.utils.storage import get_storage_for_key
from homeassistant.components.marketplace.utils.version import is_newer_version
from homeassistant.components.update.entity import _version_is_newer

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "sat"
FIRST = "5.0.0.1"
NEXT = "5.0.0.2"  # Synthetic local fixture only; never published.
COMPONENT = ROOT / "custom_components" / DOMAIN


def hashes(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def seed_current_installation(path):
    """Synthetic old-manager record with the same public configuration contract.

    The private .51 migration payload is deliberately not distributed. Exact
    production-source equivalence is verified by the separate pinned builder.
    """
    shutil.copytree(COMPONENT, path, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["version"] = "4.2.1+hodko.51"
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")



@pytest.fixture
def market(hass, tmp_path):
    # The regular pytest HA fixture is always local. No user configuration loaded.
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain="marketplace")
    entry.add_to_hass(hass)
    m = SimpleNamespace(
        hass=hass, core=SimpleNamespace(config_path=str(tmp_path)),
        configuration=SimpleNamespace(plugin_path="www/community", theme_path="themes", config_entry=entry),
        repositories=Repositories(), common=MarketplaceCommon(), system=MarketplaceSystem(),
        filesystem_lock=asyncio.Lock(), version=AwesomeVersion("2026.11.2"),
        async_dispatch=Mock(), coordinators={RepositoryCategory.INTEGRATION: Mock()},
    )
    m.common.categories.add(RepositoryCategory.INTEGRATION)
    m.data = MarketplaceData(m)
    # Unrelated installation and user config are sentinels, not production data.
    (tmp_path / "configuration.yaml").write_text("# preserved YAML identity and parameters\n")
    (tmp_path / "custom_components/sentinel").mkdir(parents=True)
    (tmp_path / "custom_components/sentinel/__init__.py").write_text("# unrelated\n")
    return m


def repository(m, monkeypatch, *, old=False):
    r = IntegrationRepository(m, "Alexwijn/SAT" if old else "offline-test/hodko-sat")
    r.data.id = "573101191" if old else "900000001"
    r.data.domain = DOMAIN
    r.data.default_branch = "codex/hodko-sat"
    r.data.releases = True
    r.data.last_version = FIRST
    r.data.last_commit = "offline-fixture"
    r.content.path.local = r.localpath
    r.repository_manifest = RepositoryManifest.from_dict({"homeassistant": "2026.11.2"})
    m.repositories.register(r)
    monkeypatch.setattr(r, "update_repository", AsyncMock())
    monkeypatch.setattr(r, "get_repository_manifest", AsyncMock(return_value=r.repository_manifest))

    async def download(version):
        # Equivalent bytes to a downloaded GitHub release, no fabricated ACK/control.
        path = Path(r.content.path.local)
        shutil.copytree(COMPONENT, path, ignore=shutil.ignore_patterns("__pycache__"))
        manifest = json.loads((path / "manifest.json").read_text())
        manifest["version"] = version
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    monkeypatch.setattr(r, "_async_download_version", download)
    return r


async def install(r, version):
    await r.async_install_repository(ref=version)
    assert r.data.installed and r.display_installed_version == version
    assert json.loads((Path(r.content.path.local) / "manifest.json").read_text())["version"] == version
    saved = await get_storage_for_key(r.marketplace.hass, "repositories").async_load()
    assert saved[r.data.id]["version_installed"] == version


async def test_standard_migration_update_and_downgrade(market, monkeypatch):
    old = repository(market, monkeypatch, old=True)
    fork = repository(market, monkeypatch)
    path = Path(old.content.path.local)
    seed_current_installation(path)
    old.data.installed = True
    old.data.installed_version = "4.2.1"  # Historical manager mismatch.
    original = hashes(path)
    with pytest.raises(MarketplaceError) as err:
        await fork.async_install_repository(ref=FIRST)
    assert err.value.translation_key == "integration_owned"
    assert hashes(path) == original
    await old.uninstall()  # Real deletion and record transition.
    await market.data.async_write()
    assert not path.exists() and not old.data.installed
    await install(fork, FIRST)
    first_hashes = hashes(path)
    fork.data.last_version = NEXT
    assert fork.pending_update
    await install(fork, NEXT)
    assert not fork.pending_update
    await install(fork, FIRST)
    assert hashes(path) == first_hashes
    # Rollback to an older release doesn't lie about the newer available release.
    assert fork.pending_update
    assert (Path(market.core.config_path) / "configuration.yaml").read_text().startswith("# preserved")
    assert (path.parent / "sentinel/__init__.py").read_text() == "# unrelated\n"


async def test_sat_and_shadow_entries_registry_and_cache_survive_repository_swap(market, monkeypatch):
    """Repository lifecycle only, not running SAT/OTGW or startup acceptance."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    hass = market.hass
    active = MockConfigEntry(domain='sat', version=10, data={'mode':'mqtt_opentherm'},
                            options={'hodko_control_mode':'ally_demand', 'maximum_setpoint':55,
                                     'hodko_ally':{'members':{'test-id':{'name':'room'}}}})
    shadow = MockConfigEntry(domain='sat', version=10, data={'mode':'ally_shadow'},
                            options={'hodko_shadow':{'members':{'test-id':{'name':'room'}}}})
    active.add_to_hass(hass); shadow.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=active.entry_id,
                                                  identifiers={('sat','test-boiler')})
    entity = er.async_get(hass).async_get_or_create('climate','sat','same-unique-id',
                                                  config_entry=active,device_id=device.id)
    storage = Path(market.core.config_path)/'.storage'
    storage.mkdir(exist_ok=True)
    cache = storage/'sat-original-report-fixture'
    contents = '{"value":87,"reported_at":"2026-11-22T08:30:00Z"}'
    cache.write_text(contents)
    old = repository(market, monkeypatch, old=True)
    seed_current_installation(Path(old.content.path.local))
    old.data.installed = True
    fork = repository(market, monkeypatch)
    await old.uninstall()
    await install(fork,FIRST)
    assert hass.config_entries.async_get_entry(active.entry_id) is active
    assert hass.config_entries.async_get_entry(shadow.entry_id) is shadow
    assert active.options['maximum_setpoint'] == 55
    assert er.async_get(hass).async_get(entity.entity_id).unique_id == 'same-unique-id'
    assert dr.async_get(hass).async_get(device.id) is not None
    assert cache.read_text() == contents


@pytest.mark.parametrize("failure", ["download", "invalid_manifest", "cancelled"])
async def test_update_failure_restores_files_and_installed_record(market, monkeypatch, failure):
    r = repository(market, monkeypatch)
    await install(r, FIRST)
    original = hashes(Path(r.content.path.local))

    async def fail(version):
        path = Path(r.content.path.local)
        path.mkdir(parents=True)
        (path / "partial.py").write_text("partial download")
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "download":
            raise OSError("injected local write failure")
        (path / "manifest.json").write_text('{"domain":"wrong"}')

    monkeypatch.setattr(r, "_async_download_version", fail)
    with pytest.raises((MarketplaceError, asyncio.CancelledError)):
        await r.async_install_repository(ref=NEXT)
    assert hashes(Path(r.content.path.local)) == original
    assert r.display_installed_version == FIRST
    saved = await get_storage_for_key(market.hass, "repositories").async_load()
    assert saved[r.data.id]["version_installed"] == FIRST
    assert not r.installing and not market.filesystem_lock.locked()


async def test_failed_first_install_cannot_undo_previous_uninstall(market, monkeypatch):
    old = repository(market, monkeypatch, old=True)
    fork = repository(market, monkeypatch)
    path = Path(old.content.path.local)
    seed_current_installation(path)
    old.data.installed = True
    await old.uninstall()

    async def fail(version):
        path.mkdir(parents=True)
        (path / "partial.py").write_text("incomplete")
        raise OSError("failed first install")

    monkeypatch.setattr(fork, "_async_download_version", fail)
    with pytest.raises(MarketplaceError):
        await fork.async_install_repository(ref=FIRST)
    assert not path.exists() and not fork.data.installed
    assert not old.data.installed  # Mandatory external backup/recovery gate.


async def test_backup_failure_does_not_replace_installed_code(market, monkeypatch):
    r = repository(market, monkeypatch)
    await install(r, FIRST)
    original = hashes(Path(r.content.path.local))
    def fail(self):
        raise MarketplaceError("injected backup failure")
    monkeypatch.setattr(Backup, "create", fail)
    download = AsyncMock()
    monkeypatch.setattr(r, "_async_download_version", download)
    with pytest.raises(MarketplaceError):
        await r.async_install_repository(ref=NEXT)
    download.assert_not_called()
    assert hashes(Path(r.content.path.local)) == original
    assert r.display_installed_version == FIRST


async def test_interrupted_transaction_restores_previous_files_on_recovery(market, monkeypatch):
    r = repository(market, monkeypatch)
    await install(r, FIRST)
    path = Path(r.content.path.local)
    original = hashes(path)
    backup = Backup(market, path)
    await market.hass.async_add_executor_job(backup.create)
    path.mkdir(parents=True)
    (path / "partial.py").write_text("simulated process death while writing")
    assert await market.hass.async_add_executor_job(restore_interrupted_backups, market)
    assert hashes(path) == original
    saved = await get_storage_for_key(market.hass, "repositories").async_load()
    assert saved[r.data.id]["version_installed"] == FIRST


@pytest.mark.parametrize("old,new,expected", [
    ("4.2.1+hodko.51", "4.2.1+hodko.52", False),
    ("4.2.1+hodko.51", FIRST, True), (FIRST, NEXT, True),
    (NEXT, "5.0.1.1", True), (FIRST, FIRST, False), (NEXT, FIRST, False),
])
def test_version_order(old, new, expected):
    assert is_newer_version(new, old) is expected
    assert _version_is_newer(new, old) is expected


def test_release_integrity_and_unpublished_guard():
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts/check_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.check(ROOT, tag=FIRST)
    with pytest.raises(ValueError):
        module.check(ROOT, tag=NEXT)
    if not json.loads((ROOT / "release-policy.json").read_text()).get("publication_ready"):
        with pytest.raises(ValueError, match="Publication is not approved"):
            module.check(ROOT, publication=True)
    else:
        module.check(ROOT, publication=True)


async def test_post_install_error_records_actual_new_files(market, monkeypatch):
    r = repository(market, monkeypatch)
    await install(r, FIRST)
    monkeypatch.setattr(r, "async_post_installation", AsyncMock(side_effect=RuntimeError("UI/cache hook failed")))
    with pytest.raises(MarketplaceError) as err:
        await r.async_install_repository(ref=NEXT)
    assert err.value.translation_key == "post_install_failed"
    assert r.display_installed_version == NEXT
    assert json.loads((Path(r.content.path.local) / "manifest.json").read_text())["version"] == NEXT
    saved = await get_storage_for_key(market.hass, "repositories").async_load()
    assert saved[r.data.id]["version_installed"] == NEXT


async def test_unsupported_ha_version_is_refused_before_write(market, monkeypatch):
    r = repository(market, monkeypatch)
    market.version = AwesomeVersion("2026.11.1")
    download = AsyncMock()
    monkeypatch.setattr(r, "_async_download_version", download)
    with pytest.raises(MarketplaceError) as err:
        await r.async_install_repository(ref=FIRST)
    assert err.value.translation_key == "requires_newer_home_assistant"
    download.assert_not_called()
    assert not Path(r.content.path.local).exists()


async def test_downgrade_restores_actual_release51_files(market, monkeypatch):
    """Real Marketplace transaction against the pinned old Git tree, not relabeling."""
    import io
    import subprocess
    import tarfile

    reference = 'd96348f2a3419baf841c3ce5ddb4bfcb6213ab78'
    archive = subprocess.check_output(['git','archive',reference,'custom_components/sat'],cwd=ROOT)
    prefix = 'custom_components/sat/'
    original = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                assert member.name.startswith(prefix)
                name = member.name[len(prefix):]
                assert '..' not in Path(name).parts
                original[name] = tar.extractfile(member).read()
    assert len(original) == 80
    r = repository(market,monkeypatch)
    await install(r,FIRST)
    path = Path(r.content.path.local)
    assert (path/'compat/climate.py').is_file()

    async def download_old(version):
        assert version == '4.2.1.51'
        for name, contents in original.items():
            p=path/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(contents)
    monkeypatch.setattr(r,'_async_download_version',download_old)
    await install(r,'4.2.1.51')
    assert hashes(path) == {k:hashlib.sha256(v).hexdigest() for k,v in original.items()}
    assert not (path/'compat').exists()
