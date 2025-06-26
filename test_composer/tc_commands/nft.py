import struct

import base58

from workload import logger

ByteLength = int

ISSUER_LENGTH: ByteLength = 20
BUFFER_LENGTH: ByteLength = 32


class BufferLengthError(ValueError):
    pass


class IssuerLengthError(ValueError):
    pass


def encode_nft_id(
    flags: int,
    transfer_fee: int,
    issuer: bytes,
    taxon: int,
    token_seq: int,
) -> str:
    """Create a unique 256-bit NFTokenID.

    000B 0539 C35B55AA096BA6D87A6E6C965A6534150DC56E5E 12C5D09E 0000000C
    +--- +--- +--------------------------------------- +------- +-------
    |    |    |                                        |        |
    |    |    |                                        |        `---> Sequence: 12
    |    |    |                                        |
    |    |    |                                        `---> Scrambled Taxon: 314,953,886
    |    |    |                                              Unscrambled Taxon: 1337
    |    |    |
    |    |    `---> Issuer: rJoxBSzpXhPtAuqFmqxQtGKjA13jUJWthE
    |    |
    |    `---> TransferFee: 1337.0 bps or 13.37%
    |
    `---> Flags: 11 -> lsfBurnable, lsfOnlyXRP and lsfTransferable

    Returns:
        A 64-character hex string (256 bits).

    Raises:
        BufferLengthError: If buffer is not 32 bytes.
        IssuerLengthError: If issuer is no 20 bytes.

    """
    issuer_bytes = base58.b58decode_check(issuer, alphabet=base58.XRP_ALPHABET)
    issuer = issuer_bytes[1:] if len(issuer_bytes) == ISSUER_LENGTH + 1 else issuer_bytes
    if len(issuer) != ISSUER_LENGTH:
        msg = f"Issuer must be {ISSUER_LENGTH} bytes"
        raise IssuerLengthError(msg)

    taxon ^= (384160001 * token_seq + 2459) & 0xFFFFFFFF
    buf = b"".join(
        [
            struct.pack(">H", flags & 0xFFFF),
            struct.pack(">H", transfer_fee & 0xFFFF),
            issuer,
            struct.pack(">I", taxon & 0xFFFFFFFF),
            struct.pack(">I", token_seq & 0xFFFFFFFF),
        ],
    )

    if len(buf) != BUFFER_LENGTH:
        msg = f"Packed buffer is not {BUFFER_LENGTH} bytes!"
        raise BufferLengthError(msg)

    nftoken_id = buf.hex().upper()
    logger.debug("NFT %s", locals())
    logger.debug("Decoded %s", nftoken_id)
    return nftoken_id
