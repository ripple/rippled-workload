"""NFToken utilities - encode NFTokenID from transaction fields."""

import base58


def scramble_taxon(taxon: int, sequence: int) -> bytes:
    """Scramble taxon with sequence using rippled's cipheredTaxon algorithm.

    See rippled/include/xrpl/protocol/nft.h
    """
    modulus = 384160001
    increment = 2459
    scramble = modulus * sequence + increment
    taxon ^= scramble
    taxon &= 0xFFFFFFFF  # Ensure 32-bit value
    return taxon.to_bytes(4, byteorder="big")


def encode_nftoken_id(
    flags: int,
    transfer_fee: int,
    issuer: str,
    taxon: int,
    sequence: int,
) -> str:
    """Encode a deterministic NFTokenID from transaction fields.

    Args:
        flags: NFToken flags (e.g., tfBurnable, tfOnlyXRP)
        transfer_fee: Transfer fee in basis points (0-50000)
        issuer: Issuer account address (e.g., "rJoxBSzpXhPtAuqFmqxQtGKjA13jUJWthE")
        taxon: NFToken taxon value
        sequence: Transaction sequence number

    Returns:
        64-character hex string (256 bits)

    Example:
        >>> encode_nftoken_id(11, 1337, "rJoxBSzpXhPtAuqFmqxQtGKjA13jUJWthE", 1337, 12)
        '000B0539C35B55AA096BA6D87A6E6C965A6534150DC56E5E12C5D09E0000000C'
    """
    issuer_bytes = base58.b58decode_check(issuer, alphabet=base58.XRP_ALPHABET)
    issuer_bytes = issuer_bytes[1:] if len(issuer_bytes) > 20 else issuer_bytes

    if len(issuer_bytes) != 20:
        raise ValueError(f"Issuer must decode to 20 bytes, got {len(issuer_bytes)}")

    taxon_bytes = scramble_taxon(taxon, sequence)

    nftoken_id_bytes = (
        flags.to_bytes(2, byteorder="big")
        + transfer_fee.to_bytes(2, byteorder="big")
        + issuer_bytes
        + taxon_bytes
        + sequence.to_bytes(4, byteorder="big")
    )

    if len(nftoken_id_bytes) != 32:
        raise ValueError(f"NFTokenID must be 32 bytes, got {len(nftoken_id_bytes)}")

    return nftoken_id_bytes.hex().upper()
