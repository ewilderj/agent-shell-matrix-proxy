"""Cross-signing key management for matrix-proxy-bot.

Ported from Casa. matrix-nio doesn't support cross-signing, so we implement
the bootstrap and signing operations manually using olm.pk.PkSigning.
"""

import copy
import json
import logging
import os
from pathlib import Path

from olm.pk import PkSigning

logger = logging.getLogger(__name__)

SEEDS_FILE = "cross_signing_seeds.json"


def _canonical_json(obj: dict) -> str:
    """Canonical JSON per Matrix spec."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _sign_json(signing: PkSigning, user_id: str, key_id: str, obj: dict) -> dict:
    """Sign *obj* per the Matrix JSON signing spec."""
    obj = dict(obj)
    sigs = obj.pop("signatures", {})
    obj.pop("unsigned", None)
    sig = signing.sign(_canonical_json(obj))
    sigs.setdefault(user_id, {})[key_id] = sig
    obj["signatures"] = sigs
    return obj


def _seeds_path(store_dir: str) -> Path:
    return Path(store_dir) / SEEDS_FILE


def _save_seeds(store_dir: str, master: bytes, self_signing: bytes, user_signing: bytes):
    """Save cross-signing seeds to disk."""
    import base64

    data = {
        "master": base64.b64encode(master).decode(),
        "self_signing": base64.b64encode(self_signing).decode(),
        "user_signing": base64.b64encode(user_signing).decode(),
    }
    path = _seeds_path(store_dir)
    path.write_text(json.dumps(data))
    path.chmod(0o600)


def _load_seeds(store_dir: str) -> dict[str, bytes] | None:
    """Load cross-signing seeds from disk."""
    import base64

    path = _seeds_path(store_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return {k: base64.b64decode(v) for k, v in data.items()}


def load_signing_keys(store_dir: str) -> dict[str, PkSigning] | None:
    """Load previously-saved cross-signing keys, or return None."""
    seeds = _load_seeds(store_dir)
    if not seeds:
        return None
    return {
        "master": PkSigning(seeds["master"]),
        "self_signing": PkSigning(seeds["self_signing"]),
        "user_signing": PkSigning(seeds["user_signing"]),
    }


async def bootstrap_cross_signing(
    client, store_dir: str, password: str
) -> dict[str, PkSigning]:
    """Generate cross-signing keys, upload them, and sign our own device.

    Returns dict of PkSigning objects keyed by role.
    """
    # Generate seeds
    master_seed = os.urandom(32)
    ss_seed = os.urandom(32)
    us_seed = os.urandom(32)

    master = PkSigning(master_seed)
    self_signing = PkSigning(ss_seed)
    user_signing = PkSigning(us_seed)

    user_id = client.user_id
    mk_id = f"ed25519:{master.public_key}"
    ss_id = f"ed25519:{self_signing.public_key}"
    us_id = f"ed25519:{user_signing.public_key}"

    # Build key objects
    master_key = _sign_json(
        master,
        user_id,
        mk_id,
        {
            "user_id": user_id,
            "usage": ["master"],
            "keys": {mk_id: master.public_key},
        },
    )

    self_signing_key = _sign_json(
        master,
        user_id,
        mk_id,
        {
            "user_id": user_id,
            "usage": ["self_signing"],
            "keys": {ss_id: self_signing.public_key},
        },
    )

    user_signing_key = _sign_json(
        master,
        user_id,
        mk_id,
        {
            "user_id": user_id,
            "usage": ["user_signing"],
            "keys": {us_id: user_signing.public_key},
        },
    )

    # Upload via UIA-authenticated endpoint
    upload_url = client.homeserver + "/_matrix/client/v3/keys/device_signing/upload"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {client.access_token}",
    }
    payload = {
        "master_key": master_key,
        "self_signing_key": self_signing_key,
        "user_signing_key": user_signing_key,
    }

    # First request without auth to get UIA session
    resp = await client.client_session.request(
        "POST", upload_url, data=json.dumps(payload),
        headers=headers, ssl=client.ssl,
    )
    if resp.status == 401:
        uia = await resp.json()
        session = uia.get("session", "")
        payload["auth"] = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": user_id},
            "password": password,
            "session": session,
        }
        resp = await client.client_session.request(
            "POST", upload_url, data=json.dumps(payload),
            headers=headers, ssl=client.ssl,
        )

    if resp.status != 200:
        text = await resp.text()
        raise RuntimeError(f"Cross-signing upload failed ({resp.status}): {text}")

    logger.info("Uploaded cross-signing keys")

    # Save seeds
    _save_seeds(store_dir, master_seed, ss_seed, us_seed)

    keys = {"master": master, "self_signing": self_signing, "user_signing": user_signing}

    # Sign our own device key with self-signing key
    await sign_own_device(client, keys)

    # Sign master key with device key (Element needs this for trust chain)
    await sign_master_key_with_device(client, keys)

    return keys


async def sign_own_device(client, keys: dict[str, PkSigning]):
    """Sign our device key with the self-signing key and upload the signature."""
    user_id = client.user_id
    device_id = client.device_id
    self_signing = keys["self_signing"]
    ss_id = f"ed25519:{self_signing.public_key}"

    # Get our device key from the server
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/query",
        data=json.dumps({"device_keys": {user_id: [device_id]}}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    data = await resp.json()
    device_key = data["device_keys"][user_id][device_id]

    # Sign it
    signed = _sign_json(self_signing, user_id, ss_id, device_key)

    # Upload the signature
    upload_body = json.dumps({user_id: {device_id: signed}})
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/signatures/upload",
        data=upload_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    if resp.status != 200:
        text = await resp.text()
        logger.warning("Device self-signature upload failed (%d): %s", resp.status, text)
    else:
        result = await resp.json()
        failures = result.get("failures", {})
        if failures:
            logger.warning("Device self-signature had failures: %s", json.dumps(failures))
        else:
            logger.info("Signed own device %s with self-signing key", device_id)


async def sign_master_key_with_device(client, keys: dict[str, PkSigning]):
    """Sign our master key with the device key, establishing device→master trust."""
    user_id = client.user_id
    device_id = client.device_id
    master = keys["master"]

    # Fetch our master key from the server
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/query",
        data=json.dumps({"device_keys": {user_id: []}}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    data = await resp.json()
    master_key = data.get("master_keys", {}).get(user_id)
    if not master_key:
        logger.warning("Could not fetch own master key for device signing")
        return

    # Sign with the device's olm account key
    signed = copy.deepcopy(master_key)
    signed.pop("signatures", None)
    signed.pop("unsigned", None)
    canon = _canonical_json(signed)
    sig = client.olm.account.sign(canon)

    # Upload with ONLY the new device signature
    upload_obj = copy.deepcopy(master_key)
    upload_obj["signatures"] = {user_id: {f"ed25519:{device_id}": sig}}

    mk_pub = master.public_key
    upload_body = json.dumps({user_id: {mk_pub: upload_obj}})
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/signatures/upload",
        data=upload_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    if resp.status != 200:
        text = await resp.text()
        logger.warning("Master key device-signature upload failed (%d): %s", resp.status, text)
    else:
        result = await resp.json()
        failures = result.get("failures", {})
        if failures:
            logger.warning("Master key device-signature had failures: %s", json.dumps(failures))
        else:
            logger.info("Signed master key with device %s", device_id)


def _inject_master_key_mac(sas, mac_dict: dict, master_key, tx_id: str) -> None:
    """Add the master cross-signing key to a SAS MAC message.

    nio's Sas.get_mac() only includes the device key. The Matrix spec
    requires the master key in the MAC for the other side to cross-sign
    it (green shield). Mutates mac_dict in place.
    """
    mk_pub = master_key.public_key
    mk_key_id = f"ed25519:{mk_pub}"

    # Use whichever MAC method the SAS session negotiated
    if sas.chosen_mac_method == sas._mac_normal:  # noqa: SLF001
        calc = sas.calculate_mac
    else:
        calc = sas.calculate_mac_long_kdf

    info = (
        "MATRIX_KEY_VERIFICATION_MAC"
        f"{sas.own_user}{sas.own_device}"
        f"{sas.other_olm_device.user_id}{sas.other_olm_device.id}"
        f"{tx_id}"
    )

    mac_dict["mac"][mk_key_id] = calc(mk_pub, info + mk_key_id)
    all_key_ids = ",".join(sorted(mac_dict["mac"].keys()))
    mac_dict["keys"] = calc(all_key_ids, info + "KEY_IDS")


async def sign_user_master_key(client, keys: dict[str, PkSigning], target_user_id: str):
    """Sign another user's master key with our user-signing key."""
    user_signing = keys["user_signing"]
    us_id = f"ed25519:{user_signing.public_key}"
    our_user_id = client.user_id

    # Fetch target user's master key
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/query",
        data=json.dumps({"device_keys": {target_user_id: []}}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    data = await resp.json()
    target_mk = data.get("master_keys", {}).get(target_user_id)
    if not target_mk:
        logger.warning("No master key found for %s — cannot cross-sign", target_user_id)
        return False

    signed = _sign_json(user_signing, our_user_id, us_id, target_mk)
    mk_pub = list(target_mk["keys"].values())[0]
    upload_body = json.dumps({target_user_id: {mk_pub: signed}})
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/signatures/upload",
        data=upload_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )
    if resp.status != 200:
        text = await resp.text()
        logger.warning("User cross-signature upload failed (%d): %s", resp.status, text)
        return False

    result = await resp.json()
    failures = result.get("failures", {})
    if failures:
        logger.warning("Cross-signature upload had failures: %s", json.dumps(failures))
        return False

    logger.info("✅ Cross-signed %s's master key", target_user_id)
    return True
