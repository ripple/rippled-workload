import argparse
import base64
import binascii
import json
from datetime import datetime, timedelta
from pathlib import Path

import xrpl

UNL_JSON = "unl.json"
UNL_DATA_JSON = "unl_data.json"

DEFAULT_NUM_VAL = 5
DEFAULT_UNL_FILE = Path(UNL_JSON)
DEFAULT_DATA_FILE = Path(UNL_DATA_JSON)

MAN_PREFIX = bytes.fromhex("4d414e00")  # b"MAN\0" ?
EXPIRES = 366  # days
ALGO = xrpl.CryptoAlgorithm.SECP256K1


def expires(days: int = EXPIRES):
    return xrpl.utils.datetime_to_ripple_time(datetime.now() + timedelta(days=days))


def gen_validator(algo: xrpl.CryptoAlgorithm = ALGO):
    master_seed = xrpl.core.keypairs.generate_seed(algorithm=algo)
    signing_seed = xrpl.core.keypairs.generate_seed(algorithm=algo)
    master_pubkey, master_privkey = xrpl.core.keypairs.derive_keypair(master_seed, validator=True, algorithm=algo)
    signing_pubkey, signing_privkey = xrpl.core.keypairs.derive_keypair(signing_seed, validator=True, algorithm=algo)

    return {
            "master_seed": master_seed,
            "signing_seed": signing_seed,
            "node_public_key": xrpl.core.addresscodec.encode_node_public_key(bytes.fromhex(master_pubkey)),
            "master_pubkey": master_pubkey,
            "master_privkey": master_privkey,
            "signing_pubkey": signing_pubkey,
            "signing_privkey": signing_privkey,
        }


def generate_validator_manifest(creds, sequence: int, domain: str | None = None):
    return gen_manifest(creds, sequence, domain, publisher=False)


def generate_publisher_manifest(creds, sequence: int, domain: str | None = None):
    return gen_manifest(creds, sequence, domain, publisher=True)


def gen_manifest(creds=None, sequence: int | None = 1, domain: str | None = None, publisher=None):
    v = creds or gen_validator()
    master_pub = v["master_pubkey"]
    signer_pub = v["signing_pubkey"]
    master_priv = v["master_privkey"]
    signing_priv = v["signing_privkey"]

    obj = {
        "PublicKey": master_pub,
        "SigningPubKey": signer_pub,
        "Sequence": sequence,
    }

    if domain is not None:
        obj["Domain"] = binascii.hexlify(domain.encode("ascii")).decode()

    unsigned_bytes = bytes.fromhex(xrpl.core.binarycodec.encode(obj))

    msg = MAN_PREFIX + unsigned_bytes
    sig_master = xrpl.core.keypairs.sign(msg, master_priv)
    sig_signing = xrpl.core.keypairs.sign(msg, signing_priv)

    final_obj = dict(obj)
    final_obj["Signature"] = sig_signing
    final_obj["MasterSignature"] = sig_master

    final_bytes = bytes.fromhex(xrpl.core.binarycodec.encode(final_obj))
    final_b64 = base64.b64encode(final_bytes).decode()
    if publisher:
        return final_b64
    return {"validation_public_key": master_pub, "manifest": final_b64}


def sign_unl_blob(publisher_creds, publisher_manifest_b64, validators, sequence, expiration=None):
    master_pubkey = publisher_creds["master_pubkey"]
    signer_privkey = publisher_creds["signing_privkey"]
    blob = {
        "sequence": sequence,
        "expiration": expiration or expires(),
        "validators": validators,
    }
    blob_bytes = json.dumps(blob, separators=(",", ":")).encode()
    sig_hex = xrpl.core.keypairs.sign(blob_bytes, signer_privkey)

    return {
        "public_key": master_pubkey,
        "manifest": publisher_manifest_b64,
        "blob": base64.b64encode(blob_bytes).decode(),
        "signature": sig_hex,
        "version": 1,
    }


def generate_unl_data(validators, publisher, sequence: int | None = None):
    sequence = sequence or 1
    validators_entries = [generate_validator_manifest(v, sequence) for v in validators]
    publisher_manifest_b64 = generate_publisher_manifest(publisher, sequence)
    return sign_unl_blob(
            publisher,
            publisher_manifest_b64,
            validators=validators_entries,
            sequence=sequence,
    )


def generate_unl(output_dir: Path, num_validators: int, verbose=False) -> dict:  # FIX: https://docs.astral.sh/ruff/rules/boolean-default-value-positional-argument
    """Write an XRPL UNL.

    Args:
        output_dir (Path): Output directory
        num_validators (int): _How many validators to generate in the list.
        verbose (bool, optional): _Writes a bunch of files of the various data.

    Returns:
        dict: The UNL data.

    """
    # TODO @legleux: Shouldn't have side-effects and return.
    num_validators = num_validators or DEFAULT_NUM_VAL
    pub_domain = "pub.xrpld"
    validators = [gen_validator() for _ in range(num_validators)]
    validators_entries = [gen_manifest(v, domain=f"val{idx}.xrpld") for idx, v in enumerate(validators)]

    publisher = gen_validator()
    publisher_manifest_b64 = gen_manifest(publisher, sequence=1, publisher=True, domain=pub_domain)

    unl = sign_unl_blob(
        publisher,
        publisher_manifest_b64,
        validators=validators_entries,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    if verbose:
        data = output_dir / "cfg_data.txt"
        unl_blob_json = output_dir / "unl_blob.json"
        unl_blob_json.write_text(json.dumps(json.loads(base64.b64decode(unl["blob"]).decode()), indent=2))
        nodes = output_dir / "node_data.json"
        nodes.write_text(json.dumps({
            "publisher": publisher,
            "publisher_manifest_b64": publisher_manifest_b64,
            "validators": validators,
            "validators_manifests": validators_entries,
        }, indent=2))

    validator_public_keys = [v["node_public_key"] for v in validators]
    validator_seeds = [v["master_seed"] for v in validators]
    validator_list_keys = publisher["master_pubkey"]

    if verbose:
        with data.open("a") as f:
            f.write("[validators]\n")
            for pk in validator_public_keys:
                f.write(pk + "\n")
            f.write("\n")
            for seed in validator_seeds:
                f.write("[validation_seed]\n")
                f.write(seed + "\n")
                f.write("\n")
            f.write("[validator_list_keys]\n")
            f.write(validator_list_keys + "\n")

    unl_json_file = output_dir / UNL_JSON
    unl_json_file.write_text(json.dumps(unl))
    if verbose:
        print(data.read_text())
        print("*********** THE UNL ***********")
        print(json.dumps(unl))
    return {
        "validator_seeds": validator_seeds,
        "validator_list_keys": validator_list_keys,
        "validator_public_keys": validator_public_keys,
    }


def parse_args():
    ap = argparse.ArgumentParser(
                    prog="XRPL UNL Manifest-er",
                    description=("Generate a quick example XRPL UNL"
                                 "Generates all credentials for publisher, validators and writes UNL."
                                 ),
                    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                    )
    ap.add_argument("-n", "--num_validators", type=int, default=DEFAULT_NUM_VAL,
                    help="Number of validators to create in UNL.",
                    )
    ap.add_argument("-o", "--output-dir", type=Path, nargs="?", default=Path().cwd(),
                    help="Dump all data to filesystem.",
                    )
    return ap.parse_args()


def main():
    args = parse_args()
    generate_unl(args.output_dir, args.num_validators)


if __name__ == "__main__":
    main()
