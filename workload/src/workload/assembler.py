"""Field-list assembler for the raw fuzz band: walk a canonical blob into ordered field
byte-spans and reassemble them, so structural ops can splice bytes the codec never emits.

Reassembly concatenates the exact spans the parser consumed, so ``reassemble(parse(b)) == b``
for any canonical blob — corruption is only ever what an operator explicitly splices."""

from __future__ import annotations

from dataclasses import dataclass

from xrpl.core.binarycodec.binary_wrappers.binary_parser import BinaryParser


@dataclass
class Field:
    name: str
    type_name: str
    is_vl: bool  # variable-length-encoded: value leads with a 1-3 byte size prefix
    header: bytes  # field-id bytes
    # everything the parser consumed after the header: VL length prefix + content for a
    # variable-length field, or nested content + end marker for an STObject/STArray.
    value: bytes

    @property
    def raw(self) -> bytes:
        return self.header + self.value


def parse(blob: bytes) -> list[Field]:
    parser = BinaryParser(blob.hex())
    fields: list[Field] = []
    while not parser.is_end():
        before = parser.bytes
        instance = parser.read_field()
        after_header = parser.bytes
        parser.read_field_value(instance)
        after_value = parser.bytes
        header = before[: len(before) - len(after_header)]
        value = after_header[: len(after_header) - len(after_value)]
        fields.append(
            Field(instance.name, instance.type, instance.is_variable_length_encoded, header, value)
        )
    return fields


def reassemble(fields: list[Field]) -> bytes:
    return b"".join(f.raw for f in fields)


def vl_prefix_len(field: Field) -> int:
    """Byte width of the VL size prefix at the head of ``field.value`` (0 if not VL).
    Mirrors rippled's length-prefix rule so an op can splice prefix vs content."""
    if not field.is_vl or not field.value:
        return 0
    first = field.value[0]
    return 1 if first <= 192 else 2 if first <= 240 else 3
