import os
import struct
from io import BytesIO
from io import StringIO
from contextlib import redirect_stdout
import textwrap
from ..common import file_size
from ..common import file_read
from .utils import Blocks
from .utils import get_matching_blocks
from ..common import unpack_size

try:
    from .. import cbsdiff as bsdiff
except ImportError:
    from .. import bsdiff as bsdiff


class DiffReader(object):

    def __init__(self,
                 ffrom,
                 to_size,
                 bw,
                 bl,
                 ldr,
                 ldr_w,
                 data_pointers,
                 code_pointers,
                 bw_blocks,
                 bl_blocks,
                 ldr_blocks,
                 ldr_w_blocks,
                 data_pointers_blocks,
                 code_pointers_blocks):
        self._ffrom = ffrom
        # ToDo: Calculate in read() for less memory usage.
        self._fdiff = BytesIO(b'\x00' * to_size)
        self._write_values_to_to(ldr_blocks, ldr)
        self._write_values_to_to(ldr_w_blocks, ldr_w)
        self._write_values_to_to(bl_blocks, bl)
        self._write_values_to_to(bw_blocks, bw)

        if data_pointers_blocks is not None:
            self._write_values_to_to(data_pointers_blocks, data_pointers)

        if code_pointers_blocks is not None:
            self._write_values_to_to(code_pointers_blocks, code_pointers)

        self._fdiff.seek(0)

    def _write_values_to_to(self, blocks, from_dict):
        from_sorted = sorted(from_dict.items())

        for from_offset, to_address, values in blocks:
            from_address_base = from_sorted[from_offset][0]

            for i, value in enumerate(values):
                from_address, from_value = from_sorted[from_offset + i]
                self._fdiff.seek(to_address + from_address - from_address_base)
                self._fdiff.write(struct.pack('<i', from_value - value))

    def read(self, size=-1):
        return self._fdiff.read(size)


class FromReader(object):

    def __init__(self,
                 ffrom,
                 bw,
                 bl,
                 ldr,
                 ldr_w,
                 data_pointers,
                 code_pointers,
                 bw_blocks,
                 bl_blocks,
                 ldr_blocks,
                 ldr_w_blocks,
                 data_pointers_blocks,
                 code_pointers_blocks):
        # ToDo: Calculate in read() for less memory usage.
        self._ffrom = BytesIO(file_read(ffrom))
        self._write_zeros_to_from(bw_blocks, bw)
        self._write_zeros_to_from(bl_blocks, bl)
        self._write_zeros_to_from(ldr_blocks, ldr)
        self._write_zeros_to_from(ldr_w_blocks, ldr_w)

        if data_pointers_blocks is not None:
            self._write_zeros_to_from(data_pointers_blocks, data_pointers)

        if code_pointers_blocks is not None:
            self._write_zeros_to_from(code_pointers_blocks, code_pointers)

    def read(self, size=-1):
        return self._ffrom.read(size)

    def seek(self, position, whence=os.SEEK_SET):
        self._ffrom.seek(position, whence)

    def _write_zeros_to_from(self, blocks, from_dict):
        from_sorted = sorted(from_dict.items())

        for from_offset, _, values in blocks:
            for i in range(len(values)):
                from_address = from_sorted[from_offset + i][0]
                self._ffrom.seek(from_address)
                self._ffrom.write(4 * b'\x00')


def create_patch_block(ffrom, fto, from_dict, to_dict):
    """Returns a bytes object of from offset, to offset, number of
    instructions and values.

    """

    from_sorted = sorted(from_dict.items())
    to_sorted = sorted(to_dict.items())
    from_addresses, from_values = zip(*from_sorted)
    to_addresses, to_values = zip(*to_sorted)
    matching_blocks = get_matching_blocks(from_addresses, to_addresses)
    blocks = Blocks()

    for from_offset, to_offset, size in matching_blocks:
        # Skip small blocks as the block overhead is too big.
        if size < 5:
            continue

        size += 1
        from_slice = from_values[from_offset:from_offset + size]
        to_slice = to_values[to_offset:to_offset + size]
        blocks.append(from_offset,
                      to_addresses[to_offset],
                      [fv - tv for fv, tv in zip(from_slice, to_slice)])

        # Overwrite blocks with zeros.
        for address in from_addresses[from_offset:from_offset + size]:
            ffrom.seek(address)
            ffrom.write(4 * b'\x00')

        for address in to_addresses[to_offset:to_offset + size]:
            fto.seek(address)
            fto.write(4 * b'\x00')

    return blocks.to_bytes()


def disassemble_data(reader,
                     address,
                     data_begin,
                     data_end,
                     code_begin,
                     code_end,
                     data_pointers,
                     code_pointers):
    value = struct.unpack('<I', reader.read(4))[0]

    if data_begin <= value < data_end:
        data_pointers[address] = value
    elif code_begin <= value < code_end:
        code_pointers[address] = value


def disassemble_bw_bl(reader, address, bw, bl, upper_16):
    lower_16 = struct.unpack('<H', reader.read(2))[0]

    if (lower_16 & 0xd000) == 0xd000:
        imm32 = ((upper_16 & 0x3ff) << 12)
        imm32 += ((lower_16 & 0x7ff) << 1)
        imm32 += 4

        if imm32 & (1 << 21):
            imm32 -= (1 << 22)

        bl[address] = imm32
        # ToDo: Use imm32 for smaller patches.
        reader.seek(-4, os.SEEK_CUR)
        bl[address] = struct.unpack('<i', reader.read(4))[0]
    elif (lower_16 & 0xc000) == 0x8000:
        reader.seek(-4, os.SEEK_CUR)
        bw[address] = struct.unpack('<i', reader.read(4))[0]


def disassemble_ldr_common(reader, address, ldr, imm):
    if (address % 4) == 2:
        address -= 2

    address += imm
    position = reader.tell()
    reader.seek(address)
    ldr[address] = struct.unpack('<i', reader.read(4))[0]
    reader.seek(position)


def disassemble_ldr(reader, address, ldr, upper_16):
    imm8 = 4 * (upper_16 & 0xff) + 4
    disassemble_ldr_common(reader, address, ldr, imm8)


def disassemble_ldr_w(reader, address, ldr_w):
    lower_16 = struct.unpack('<H', reader.read(2))[0]
    imm12 = (lower_16 & 0xfff) + 4
    disassemble_ldr_common(reader, address, ldr_w, imm12)


def disassemble(reader,
                data_offset,
                data_begin,
                data_end,
                code_begin,
                code_end):
    """Disassembles given data and returns address-value pairs of b.w, bl,
    *ldr and *ldr.w.

1f5d945af missed:

 8023760:	68b3            ldr	r3, [r6, #8]
 8023762:	091b            lsrs	r3, r3, #4
 8023764:	e001            b.n	802376a <scope_new+0x2e>
 -> 8023766:	4b08            ldr	r3, [pc, #32]	; (8023788 <scope_new+0x4c>)

erroneously overwritten with zeros:

 802c3c4:	4630            mov	r0, r6
 802c3c6:	f00c fefb       bl	80391c0 <mp_small_int_floor_divide>

not overwritten:

 802e92e:	bf08            it	eq
 802e930:	4615            moveq	r5, r2
 802e932:	4a21            ldr	r2, [pc, #132]	; (802e9b8 <dict_print+0x94>)

 802e9b4:	e8bd 87f0       ldmia.w	sp!, {r4, r5, r6, r7, r8, r9, sl, pc}
 -> 802e9b8:	0805d8c8        .word	0x0805d8c8

 8045a7e:	4602            mov	r2, r0
 8045a80:	d905            bls.n	8045a8e <led_obj_make_new+0x2a>
 8045a82:	4905            ldr	r1, [pc, #20]	; (8045a98 <led_obj_make_new+0x34>)

 8045a96:	bd10            pop	{r4, pc}
 -> 8045a98:	08068b5c        .word	0x08068b5c

    """

    length = file_size(reader)
    bw = {}
    bl = {}
    ldr = {}
    ldr_w = {}
    data_pointers = {}
    code_pointers = {}
    data_offset_end = (data_offset + data_end - data_begin)

    while reader.tell() < length:
        address = reader.tell()

        if data_offset <= address < data_offset_end:
            disassemble_data(reader,
                             address,
                             data_begin,
                             data_end,
                             code_begin,
                             code_end,
                             data_pointers,
                             code_pointers)
        elif address in ldr or address in ldr_w:
            reader.read(4)
        else:
            upper_16 = struct.unpack('<H', reader.read(2))[0]

            if (upper_16 & 0xf800) == 0xf000:
                disassemble_bw_bl(reader, address, bw, bl, upper_16)
            elif (upper_16 & 0xfff0) == 0xfbb0:
                reader.read(2)
            elif (upper_16 & 0xf800) == 0x4800:
                disassemble_ldr(reader, address, ldr, upper_16)
            elif upper_16 == 0xf8df:
                disassemble_ldr_w(reader, address, ldr_w)
                
    return bw, bl, ldr, ldr_w, data_pointers, code_pointers


def cortex_m4_encode(ffrom,
                     fto,
                     from_data_offset,
                     from_data_begin,
                     from_data_end,
                     from_code_begin,
                     from_code_end,
                     to_data_offset,
                     to_data_begin,
                     to_data_end,
                     to_code_begin,
                     to_code_end):
    ffrom = BytesIO(file_read(ffrom))
    fto = BytesIO(file_read(fto))
    (from_bw,
     from_bl,
     from_ldr,
     from_ldr_w,
     from_data_pointers,
     from_code_pointers) = disassemble(ffrom,
                                       from_data_offset,
                                       from_data_begin,
                                       from_data_end,
                                       from_code_begin,
                                       from_code_end)
    (to_bw,
     to_bl,
     to_ldr,
     to_ldr_w,
     to_data_pointers,
     to_code_pointers) = disassemble(fto,
                                     to_data_offset,
                                     to_data_begin,
                                     to_data_end,
                                     to_code_begin,
                                     to_code_end)

    if from_data_end == 0:
        patch = b'\x00'
    else:
        patch = b'\x01'
        patch += bsdiff.pack_size(from_data_offset)
        patch += bsdiff.pack_size(from_data_begin)
        patch += bsdiff.pack_size(from_data_end)
        patch += create_patch_block(ffrom,
                                    fto,
                                    from_data_pointers,
                                    to_data_pointers)

    if from_code_end == 0:
        patch += b'\x00'
    else:
        patch += b'\x01'
        patch += bsdiff.pack_size(from_code_begin)
        patch += bsdiff.pack_size(from_code_end)
        patch += create_patch_block(ffrom,
                                    fto,
                                    from_code_pointers,
                                    to_code_pointers)

    patch += create_patch_block(ffrom, fto, from_bw, to_bw)
    patch += create_patch_block(ffrom, fto, from_bl, to_bl)
    patch += create_patch_block(ffrom, fto, from_ldr, to_ldr)
    patch += create_patch_block(ffrom, fto, from_ldr_w, to_ldr_w)

    return ffrom, fto, patch


def cortex_m4_create_readers(ffrom, patch, to_size):
    """Return diff and from readers, used when applying a patch.

    """

    fpatch = BytesIO(patch)
    data_pointers_blocks_present = (fpatch.read(1) == b'\x01')

    if data_pointers_blocks_present:
        from_data_offset = unpack_size(fpatch)[0]
        from_data_begin = unpack_size(fpatch)[0]
        from_data_end = unpack_size(fpatch)[0]
        data_pointers_blocks = Blocks.from_fpatch(fpatch)
    else:
        from_data_offset = 0
        from_data_begin = 0
        from_data_end = 0
        data_pointers_blocks = None

    code_pointers_blocks_present = (fpatch.read(1) == b'\x01')

    if code_pointers_blocks_present:
        from_code_begin = unpack_size(fpatch)[0]
        from_code_end = unpack_size(fpatch)[0]
        code_pointers_blocks = Blocks.from_fpatch(fpatch)
    else:
        from_code_begin = 0
        from_code_end = 0
        code_pointers_blocks = None

    bw_blocks = Blocks.from_fpatch(fpatch)
    bl_blocks = Blocks.from_fpatch(fpatch)
    ldr_blocks = Blocks.from_fpatch(fpatch)
    ldr_w_blocks = Blocks.from_fpatch(fpatch)
    (bw,
     bl,
     ldr,
     ldr_w,
     data_pointers,
     code_pointers) = disassemble(ffrom,
                                  from_data_offset,
                                  from_data_begin,
                                  from_data_end,
                                  from_code_begin,
                                  from_code_end)
    diff_reader = DiffReader(ffrom,
                             to_size,
                             bw,
                             bl,
                             ldr,
                             ldr_w,
                             data_pointers,
                             code_pointers,
                             bw_blocks,
                             bl_blocks,
                             ldr_blocks,
                             ldr_w_blocks,
                             data_pointers_blocks,
                             code_pointers_blocks)
    from_reader = FromReader(ffrom,
                             bw,
                             bl,
                             ldr,
                             ldr_w,
                             data_pointers,
                             code_pointers,
                             bw_blocks,
                             bl_blocks,
                             ldr_blocks,
                             ldr_w_blocks,
                             data_pointers_blocks,
                             code_pointers_blocks)

    return diff_reader, from_reader


def format_blocks(blocks, blocks_size, fsize):
    print('Number of blocks:   {}'.format(len(blocks)))
    print('Size:               {}'.format(fsize(blocks_size)))
    print()

    for i, (from_offset, to_address, values) in enumerate(blocks):
        print('------------------- Block {} -------------------'.format(i + 1))
        print()
        print('From offset:        {}'.format(from_offset))
        print('To address:         0x{:x}'.format(to_address))
        print('Number of values:   {}'.format(len(values)))
        print('Values:')
        lines = textwrap.wrap(' '.join([str(value) for value in values]))
        lines = ['  ' + line for line in lines]
        print('\n'.join(lines))
        print()


def load_blocks(fpatch):
    position = fpatch.tell()
    blocks = Blocks.from_fpatch(fpatch)
    blocks_size = fpatch.tell() - position

    return blocks, blocks_size


def cortex_m4_info(patch, fsize):
    fpatch = BytesIO(patch)
    data_pointers_blocks_present = (fpatch.read(1) == b'\x01')

    if data_pointers_blocks_present:
        from_data_offset = unpack_size(fpatch)[0]
        from_data_begin = unpack_size(fpatch)[0]
        from_data_end = unpack_size(fpatch)[0]
        data_pointers_blocks, data_pointers_blocks_size = load_blocks(fpatch)

    code_pointers_blocks_present = (fpatch.read(1) == b'\x01')

    if code_pointers_blocks_present:
        from_code_begin = unpack_size(fpatch)[0]
        from_code_end = unpack_size(fpatch)[0]
        code_pointers_blocks, code_pointers_blocks_size = load_blocks(fpatch)

    bw_blocks, bw_blocks_size = load_blocks(fpatch)
    bl_blocks, bl_blocks_size = load_blocks(fpatch)
    ldr_blocks, ldr_blocks_size = load_blocks(fpatch)
    ldr_w_blocks, ldr_w_blocks_size = load_blocks(fpatch)
    fout = StringIO()

    with redirect_stdout(fout):
        print('Instruction:        b.w')
        format_blocks(bw_blocks, bw_blocks_size, fsize)
        print('Instruction:        bl')
        format_blocks(bl_blocks, bl_blocks_size, fsize)
        print('Instruction:        ldr')
        format_blocks(ldr_blocks, ldr_blocks_size, fsize)
        print('Instruction:        ldr.w')
        format_blocks(ldr_w_blocks, ldr_w_blocks_size, fsize)

        if data_pointers_blocks_present:
            print('Kind:               data-pointers')
            print('From data offset:   0x{:x}'.format(from_data_offset))
            print('From data begin:    0x{:x}'.format(from_data_begin))
            print('From data end:      0x{:x}'.format(from_data_end))
            format_blocks(data_pointers_blocks,
                          data_pointers_blocks_size,
                          fsize)

        if code_pointers_blocks_present:
            print('Kind:               code-pointers')
            print('From code begin:    0x{:x}'.format(from_code_begin))
            print('From code end:      0x{:x}'.format(from_code_end))
            format_blocks(code_pointers_blocks,
                          code_pointers_blocks_size,
                          fsize)

    return fout.getvalue()