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
        fields.append(Field(instance.name, instance.type, header, value))
    return fields


def reassemble(fields: list[Field]) -> bytes:
    return b"".join(f.raw for f in fields)
