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

    # Upload keys
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/device_signing/upload",
        data=json.dumps(
            {
                "master_key": master_key,
                "self_signing_key": self_signing_key,
                "user_signing_key": user_signing_key,
            }
        ),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )

    if resp.status != 200:
        text = await resp.text()
        raise RuntimeError(f"Cross-signing upload failed ({resp.status}): {text}")

    logger.info("Uploaded cross-signing keys")

    # Sign our own device with self-signing key
    device_id = client.device_id
    olm_account = client.olm.account
    device_key = list(olm_account.curve25519_keys.values())[0]

    device_sig = _sign_json(
        self_signing,
        user_id,
        ss_id,
        {
            "user_id": user_id,
            "device_id": device_id,
            "keys": {f"curve25519:{device_id}": device_key},
        },
    )

    # Upload device signature
    resp = await client.client_session.request(
        "POST",
        client.homeserver + "/_matrix/client/v3/keys/signatures/upload",
        data=json.dumps({user_id: {device_key: device_sig}}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.access_token}",
        },
        ssl=client.ssl,
    )

    if resp.status != 200:
        logger.warning(f"Device signature upload failed ({resp.status})")
    else:
        logger.info("Signed own device")

    # Save seeds
    _save_seeds(store_dir, master_seed, ss_seed, us_seed)

    return {
        "master": master,
        "self_signing": self_signing,
        "user_signing": user_signing,
    }


def _inject_master_key_mac(sas, mac_dict: dict, master_key, tx_id: str) -> None:
    """Add the master cross-signing key to a SAS MAC message.

    nio's Sas.get_mac() only includes the device key. The Matrix spec
    requires the master key in the MAC for the other side to cross-sign
    it (green shield).
    """
    user_id = sas.user_id
    mk_pub = master_key.public_key
    mk_id = f"ed25519:{mk_pub}"

    # Compute MAC for master key
    mac_input = f"{sas.mac_info}{mk_id}{mk_pub}{tx_id}"
    mac = sas.compute_mac(mac_input)

    # Add to MAC dict
    mac_dict["master_keys"] = {mk_id: mac}
