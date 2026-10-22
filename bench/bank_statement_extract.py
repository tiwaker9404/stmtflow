import re
import os
import sys
import csv
import subprocess
import tempfile
from collections import defaultdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def unescape_pdf_text(s: str) -> str:
    s = s.replace(r'\(', '(').replace(r'\)', ')')
    s = s.replace(r'\\', '\\').replace(r'\n', '\n')
    s = s.replace(r'\r', '\r').replace(r'\t', '\t')
    return s


def is_numeric(text: str) -> bool:
    t = text.strip()
    return bool(re.match(r'^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$', t))


def parse_amount_text(text: str):
    """
    Parse amount-like strings and return normalized numeric text, or None.
    Supports common statement variants like commas, CR/DR suffixes, and trailing '-'.
    """
    t = text.strip()
    if not t:
        return None

    t = t.replace('₹', '').replace('INR', '').replace('Rs.', '').replace('Rs', '').strip()
    sign = 1

    if t.startswith('(') and t.endswith(')'):
        sign = -1
        t = t[1:-1].strip()

    upper = t.upper()
    if upper.endswith('CR') or upper.endswith('DR'):
        t = t[:-2].strip()
        upper = t.upper()
    if upper.startswith('CR') or upper.startswith('DR'):
        t = t[2:].strip()

    if t.endswith('-'):
        sign = -1
        t = t[:-1].strip()

    t = t.replace(',', '').replace(' ', '')
    if not re.match(r'^-?\d+(?:\.\d+)?$', t):
        return None

    if sign < 0 and not t.startswith('-'):
        t = '-' + t
    return t


def snap_y(y: float, tolerance: float = 1.5) -> float:
    return round(y / tolerance) * tolerance


def parse_float_arg(flag: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{flag} expects a numeric value, got: {value}") from exc


def parse_amount_columns_arg(value: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in value.split(',')]
    if len(parts) != 3:
        raise ValueError("--amount-columns expects 3 comma-separated values: deposit_min,withdraw_min,balance_min")

    try:
        deposit_min, withdraw_min, balance_min = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError("--amount-columns values must be numeric") from exc

    if not (deposit_min < withdraw_min < balance_min):
        raise ValueError("--amount-columns must satisfy deposit_min < withdraw_min < balance_min")

    return deposit_min, withdraw_min, balance_min


# ---------------------------------------------------------------------------
# Stage 1 – extract blocks preserving stream order
# ---------------------------------------------------------------------------

BT_ET_RE = re.compile(r'BT\b([\s\S]*?)\bET\b', re.MULTILINE)
TM_RE = re.compile(r'(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+Tm')
TJ_RE = re.compile(r'\(((?:[^()\\]|\\.)*)\)\s*Tj')
TJ_ARR_RE = re.compile(r'\[(.*?)\]\s*TJ', re.DOTALL)
STR_ARR_RE = re.compile(r'\(((?:[^()\\]|\\.)*)\)')

OBJ_RE = re.compile(r'(?ms)^(\d+)\s+0\s+obj\b(.*?)^endobj\b')
FONT_REF_RE = re.compile(r'/(F\d+)\s+(\d+)\s+0\s+R')
TYPE0_TOUNICODE_RE = re.compile(r'/Subtype\s*/Type0\b.*?/ToUnicode\s+(\d+)\s+0\s+R', re.S)
FONT_SELECT_RE = re.compile(r'/([A-Za-z0-9]+)\s+[-+]?\d*\.?\d+\s+Tf')
HEX_TJ_RE = re.compile(r'<([0-9A-Fa-f\s]+)>\s*Tj')
ARR_TOKEN_RE = re.compile(r'\(((?:[^()\\]|\\.)*)\)|<([0-9A-Fa-f\s]+)>')
BFCHAR_BLOCK_RE = re.compile(r'(\d+)\s+beginbfchar(.*?)endbfchar', re.S)
BFRANGE_BLOCK_RE = re.compile(r'(\d+)\s+beginbfrange(.*?)endbfrange', re.S)
BFCHAR_LINE_RE = re.compile(r'<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>')
BFRANGE_LINE_RE = re.compile(r'<([0-9A-Fa-f]+)>\s+<([0-9A-Fa-f]+)>\s+(<[^>]+>|\[[^\]]+\])')
HEX_IN_ARRAY_RE = re.compile(r'<([0-9A-Fa-f]+)>')


def extract_stream(body: str) -> str:
    if 'stream' not in body or 'endstream' not in body:
        return ''

    start = body.index('stream') + len('stream')
    if start < len(body) and body[start:start + 2] == '\r\n':
        start += 2
    elif start < len(body) and body[start] in '\r\n':
        start += 1

    end = body.rfind('endstream')
    return body[start:end]


def decode_utf16_hex(hex_text: str) -> str:
    clean = re.sub(r'\s+', '', hex_text)
    if len(clean) % 2:
        clean = clean[:-1]
    if not clean:
        return ''

    try:
        return bytes.fromhex(clean).decode('utf-16-be', errors='ignore')
    except ValueError:
        return ''


def parse_tounicode_cmap(stream_text: str) -> dict:
    cmap = {}

    for _, block in BFCHAR_BLOCK_RE.findall(stream_text):
        for src_hex, dst_hex in BFCHAR_LINE_RE.findall(block):
            cmap[src_hex.upper()] = decode_utf16_hex(dst_hex)

    for _, block in BFRANGE_BLOCK_RE.findall(stream_text):
        for start_hex, end_hex, dst_spec in BFRANGE_LINE_RE.findall(block):
            start = int(start_hex, 16)
            end = int(end_hex, 16)

            if dst_spec.startswith('['):
                dests = [decode_utf16_hex(x) for x in HEX_IN_ARRAY_RE.findall(dst_spec)]
                for offset, code in enumerate(range(start, end + 1)):
                    if offset < len(dests):
                        cmap[f"{code:0{len(start_hex)}X}"] = dests[offset]
                continue

            dst_start = int(dst_spec[1:-1], 16)
            for offset, code in enumerate(range(start, end + 1)):
                dst_code = dst_start + offset
                cmap[f"{code:0{len(start_hex)}X}"] = decode_utf16_hex(f"{dst_code:04X}")

    return cmap


def build_font_maps(content: str) -> dict:
    objects = {int(obj_id): body for obj_id, body in OBJ_RE.findall(content)}
    font_alias_to_obj = {}

    for body in objects.values():
        for alias, obj_id in FONT_REF_RE.findall(body):
            font_alias_to_obj[alias] = int(obj_id)

    font_obj_to_cmap = {}
    for obj_id, body in objects.items():
        match = TYPE0_TOUNICODE_RE.search(body)
        if not match:
            continue

        cmap_obj = int(match.group(1))
        cmap_body = objects.get(cmap_obj, '')
        cmap_stream = extract_stream(cmap_body)
        if cmap_stream:
            font_obj_to_cmap[obj_id] = parse_tounicode_cmap(cmap_stream)

    return {
        alias: font_obj_to_cmap.get(obj_id, {})
        for alias, obj_id in font_alias_to_obj.items()
    }


def decode_hex_text(hex_text: str, cmap: dict) -> str:
    clean = re.sub(r'\s+', '', hex_text).upper()
    if not clean:
        return ''

    if not cmap:
        return decode_utf16_hex(clean)

    key_lengths = sorted({len(k) for k in cmap.keys()}, reverse=True)
    out = []
    i = 0

    while i < len(clean):
        matched = False
        for key_len in key_lengths:
            part = clean[i:i + key_len]
            if len(part) == key_len and part in cmap:
                out.append(cmap[part])
                i += key_len
                matched = True
                break

        if matched:
            continue

        if i + 4 <= len(clean):
            out.append(decode_utf16_hex(clean[i:i + 4]))
            i += 4
        else:
            break

    return ''.join(out)


def extract_segment_text(segment: str, cmap: dict) -> str:
    texts = []

    for tj in TJ_RE.finditer(segment):
        texts.append(unescape_pdf_text(tj.group(1)))

    for tj in HEX_TJ_RE.finditer(segment):
        texts.append(decode_hex_text(tj.group(1), cmap))

    for tjarr in TJ_ARR_RE.finditer(segment):
        for lit, hex_text in ARR_TOKEN_RE.findall(tjarr.group(1)):
            if lit:
                texts.append(unescape_pdf_text(lit))
            elif hex_text:
                texts.append(decode_hex_text(hex_text, cmap))

    return ''.join(texts).strip()


def extract_blocks_from_page(page_content: str,
                             page_index: int,
                             stream_offset: int = 0,
                             font_maps: dict | None = None) -> list:
    blocks = []
    font_positions = [(m.start(), m.group(1)) for m in FONT_SELECT_RE.finditer(page_content)] if font_maps else []

    for bt_seq, bt_match in enumerate(BT_ET_RE.finditer(page_content)):
        inner = bt_match.group(1)
        tm_positions = [(m.start(), m.end(), m) for m in TM_RE.finditer(inner)]
        if not tm_positions:
            continue

        inner_font_positions = [(m.start(), m.group(1)) for m in FONT_SELECT_RE.finditer(inner)] if font_maps else []

        for idx, (tm_start, tm_end, tm_match) in enumerate(tm_positions):
            x = float(tm_match.group(5))
            y = float(tm_match.group(6))
            seg_end = tm_positions[idx + 1][0] if idx + 1 < len(tm_positions) else len(inner)
            segment = inner[tm_end:seg_end]

            cmap = {}
            if font_maps:
                active_font = None
                for pos, font_name in inner_font_positions:
                    if pos <= tm_start:
                        active_font = font_name
                    else:
                        break
                cmap = font_maps.get(active_font, {})

            combined = extract_segment_text(segment, cmap) if font_maps else ''
            if not combined:
                texts = []
                for tj in TJ_RE.finditer(segment):
                    texts.append(unescape_pdf_text(tj.group(1)))
                for tjarr in TJ_ARR_RE.finditer(segment):
                    for s in STR_ARR_RE.finditer(tjarr.group(1)):
                        texts.append(unescape_pdf_text(s.group(1)))
                combined = ''.join(texts).strip()

            if not combined:
                continue

            blocks.append({
                'x': round(x, 2),
                'y': snap_y(y),
                'raw_y': y,
                'text': combined,
                'page': page_index,
                'stream_index': stream_offset + bt_seq,
            })

    return blocks


def decompress_pdf(input_path: str) -> str:
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"PDF not found: {input_path}")

    tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False, prefix='qpdf_decoded_')
    tmp.close()
    out_path = tmp.name

    cmd = ['qpdf', '--decode-level=all', '--stream-data=uncompress',
           '--object-streams=disable', input_path, out_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 3):
        os.unlink(out_path)
        raise RuntimeError(f"qpdf failed (exit {result.returncode}):\n{result.stderr.strip()}")
    if result.stderr.strip():
        print(f"[qpdf] {result.stderr.strip()}", file=sys.stderr)
    return out_path


def extract_all_blocks(file_path: str, use_tounicode: bool = False) -> list:
    decoded_path = decompress_pdf(file_path)
    try:
        with open(decoded_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    finally:
        os.unlink(decoded_path)

    font_maps = build_font_maps(content) if use_tounicode else None

    page_splits = list(re.finditer(r'\bstream\r?\n', content))
    if len(page_splits) <= 1:
        return extract_blocks_from_page(content, 0, 0, font_maps)

    all_blocks = []
    stream_offset = 0
    for i, ps in enumerate(page_splits):
        end = page_splits[i + 1].start() if i + 1 < len(page_splits) else len(content)
        page_text = content[ps.start():end]
        page_blocks = extract_blocks_from_page(page_text, i, stream_offset, font_maps)
        all_blocks.extend(page_blocks)
        stream_offset += len(list(BT_ET_RE.finditer(page_text)))

    return all_blocks


# ---------------------------------------------------------------------------
# Stage 2 – transaction parsing based on observed PDF structure
# ---------------------------------------------------------------------------

DATE_RE = re.compile(
    r'\b\d{2}-(?:\d{2}|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2}(?:\d{2})?\b',
    re.IGNORECASE
)


def parse_transactions(blocks: list,
                       date_x_max: float = 100.0,
                       desc_x_min: float = 50.0,
                       desc_x_max: float = 350.0,
                       deposit_x_min: float = 350.0,
                       deposit_x_max: float = 420.0,
                       withdraw_x_min: float = 420.0,
                       withdraw_x_max: float = 500.0,
                       balance_x_min: float = 500.0,
                       amount_y_tolerance: float = 5.0,
                       desc_lookahead: int = 3,
                       verbose: bool = False) -> list:
    """
    Transaction parser built around the observed stream structure.

    For each DATE block found:
      - Amounts: same Y as date, X in amount columns
      - Description: the next stream index(es) after date that contain
        text in the description X range and no date / no amount-column numbers.
    """
    blocks_stream = sorted(blocks, key=lambda b: b['stream_index'])

    by_si = defaultdict(list)
    for b in blocks_stream:
        by_si[b['stream_index']].append(b)

    si_list = sorted(by_si.keys())
    transactions = []
    consumed_si = set()

    def vprint(msg: str):
        if verbose:
            print(msg)

    def classify_amount_by_x(x: float):
        if x >= balance_x_min:
            return 'balance'
        if deposit_x_min <= x < deposit_x_max:
            return 'deposit'
        if withdraw_x_min <= x < withdraw_x_max:
            return 'withdrawal'
        return None

    for si_pos, si in enumerate(si_list):
        if si in consumed_si:
            continue

        si_blocks = by_si[si]
        page = si_blocks[0]['page']

        date_block = None
        for b in si_blocks:
            if DATE_RE.search(b['text']) and b['x'] < date_x_max:
                date_block = b
                break
        if date_block is None:
            continue

        found_date = DATE_RE.search(date_block['text']).group(0)
        date_y = date_block['y']
        consumed_si.add(si)
        vprint(
            f"[detect] date={found_date} page={page} si={si} y={date_y:.1f} "
            f"x={date_block['x']:.2f} text='{date_block['text'][:80]}'"
        )

        found_amounts = {'deposit': '', 'withdrawal': '', 'balance': ''}

        for b in si_blocks:
            parsed_amount = parse_amount_text(b['text'])
            if abs(b['y'] - date_y) < amount_y_tolerance and parsed_amount is not None:
                bucket = classify_amount_by_x(b['x'])
                if bucket:
                    found_amounts[bucket] = parsed_amount
                    vprint(f"[amount] si={si} y={b['y']:.1f} x={b['x']:.2f} -> {bucket}={parsed_amount} (same-si)")

        desc_lines = []

        for fwd_pos in range(si_pos + 1, min(si_pos + 1 + desc_lookahead + 10, len(si_list))):
            fsi = si_list[fwd_pos]
            if fsi in consumed_si:
                continue

            fblocks = by_si[fsi]
            fpage = fblocks[0]['page']
            if fpage != page:
                break

            amount_blocks = [
                b for b in fblocks
                if abs(b['y'] - date_y) < amount_y_tolerance
                and parse_amount_text(b['text']) is not None
                and b['x'] >= deposit_x_min
            ]

            desc_blocks = [
                b for b in fblocks
                if desc_x_min <= b['x'] <= desc_x_max
                and not DATE_RE.search(b['text'])
                and not (parse_amount_text(b['text']) is not None and b['x'] >= deposit_x_min)
            ]

            has_new_date = any(
                DATE_RE.search(b['text']) and b['x'] < date_x_max
                for b in fblocks
            )
            if has_new_date:
                vprint(f"[stop] encountered next date candidate at si={fsi}; ending lookahead for si={si}")
                break

            if amount_blocks:
                consumed_si.add(fsi)
                for b in amount_blocks:
                    bucket = classify_amount_by_x(b['x'])
                    if not bucket:
                        continue
                    parsed_amount = parse_amount_text(b['text'])
                    if parsed_amount is None:
                        continue
                    found_amounts[bucket] = parsed_amount
                    vprint(f"[amount] si={fsi} y={b['y']:.1f} x={b['x']:.2f} -> {bucket}={parsed_amount}")

            if desc_blocks:
                consumed_si.add(fsi)
                for b in sorted(desc_blocks, key=lambda b: -b['y']):
                    line = b['text'].strip()
                    if line:
                        desc_lines.append(line)
                        vprint(f"[desc] si={fsi} y={b['y']:.1f} x={b['x']:.2f} '{line[:100]}'")

        deposit = found_amounts['deposit']
        withdrawal = found_amounts['withdrawal']
        balance = found_amounts['balance']

        if not balance:
            for b in si_blocks:
                parsed_amount = parse_amount_text(b['text'])
                if parsed_amount is not None and b['x'] >= balance_x_min:
                    balance = parsed_amount
                    vprint(f"[fallback] same-si balance from x={b['x']:.2f} y={b['y']:.1f} -> {balance}")

        description = ' '.join(desc_lines).strip()

        if found_date and (balance or deposit or withdrawal):
            vprint(
                f"[tx] date={found_date} withdrawal={withdrawal or '-'} "
                f"deposit={deposit or '-'} balance={balance or '-'} "
                f"desc_lines={len(desc_lines)}"
            )
            transactions.append({
                'date': found_date,
                'description': description,
                'deposit': deposit,
                'withdrawal': withdrawal,
                'balance': balance,
            })
        else:
            vprint(f"[skip] date={found_date} had no usable amounts")

    return transactions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bank_statement(file_path: str,
                         date_x_max: float = 100.0,
                         desc_x_min: float = 50.0,
                         desc_x_max: float = 350.0,
                         deposit_x_min: float = 350.0,
                         deposit_x_max: float = 420.0,
                         withdraw_x_min: float = 420.0,
                         withdraw_x_max: float = 500.0,
                         balance_x_min: float = 500.0,
                         amount_y_tolerance: float = 5.0,
                         use_tounicode: bool = False,
                         verbose: bool = False) -> list:
    def run_parser(tounicode_enabled: bool) -> tuple[list, list]:
        blocks = extract_all_blocks(file_path, use_tounicode=tounicode_enabled)
        if verbose:
            mode = 'on' if tounicode_enabled else 'off'
            print(f"[path] qpdf-stream blocks mode=tounicode:{mode} count={len(blocks)}")

        rows = parse_transactions(
            blocks,
            date_x_max=date_x_max,
            desc_x_min=desc_x_min,
            desc_x_max=desc_x_max,
            deposit_x_min=deposit_x_min,
            deposit_x_max=deposit_x_max,
            withdraw_x_min=withdraw_x_min,
            withdraw_x_max=withdraw_x_max,
            balance_x_min=balance_x_min,
            amount_y_tolerance=amount_y_tolerance,
            verbose=verbose,
        )
        return blocks, rows

    _, rows = run_parser(use_tounicode)
    if rows or use_tounicode:
        return rows

    if verbose:
        print("[path] retrying with /ToUnicode decoding")

    _, rows = run_parser(True)
    return rows


# ---------------------------------------------------------------------------
# Debug – stream order view
# ---------------------------------------------------------------------------

def debug_blocks(file_path: str, use_tounicode: bool = False):
    blocks = extract_all_blocks(file_path, use_tounicode=use_tounicode)
    print(f"{'SI':>5}  {'PG':>3}  {'Y':>7}  {'X':>7}  TEXT")
    print('-' * 72)
    for b in sorted(blocks, key=lambda b: (b['stream_index'], -b['y'])):
        marker = ' <DATE>' if DATE_RE.search(b['text']) else \
                 ' <AMT>' if is_numeric(b['text']) else ''
        print(f"{b['stream_index']:>5}  {b['page']:>3}  {b['y']:>7.1f}  "
              f"{b['x']:>7.2f}  {b['text'][:55]}{marker}")


def export_transactions_csv(rows: list, output_path: str):
    fieldnames = ['date', 'description', 'withdrawal', 'deposit', 'balance']
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, '') for k in fieldnames})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    args = sys.argv[1:]
    pdf_path = 'statement.pdf'
    verbose = False
    debug = False
    csv_path = None
    use_tounicode = False

    date_x_max = 100.0
    desc_x_min = 50.0
    desc_x_max = 350.0
    deposit_x_min = 350.0
    deposit_x_max = 420.0
    withdraw_x_min = 420.0
    withdraw_x_max = 500.0
    balance_x_min = 500.0
    amount_y_tolerance = 5.0

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--verbose':
            verbose = True
        elif arg == '--debug':
            debug = True
        elif arg == '--tounicode':
            use_tounicode = True
        elif arg == '--amount-columns':
            if i + 1 >= len(args):
                print("Error: --amount-columns requires a value like 420,560,650", file=sys.stderr)
                sys.exit(2)
            try:
                deposit_x_min, withdraw_x_min, balance_x_min = parse_amount_columns_arg(args[i + 1])
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)
            deposit_x_max = withdraw_x_min
            withdraw_x_max = balance_x_min
            i += 1
        elif arg == '--csv':
            if i + 1 >= len(args):
                print("Error: --csv requires an output file path.", file=sys.stderr)
                sys.exit(2)
            csv_path = args[i + 1]
            i += 1
        elif arg in {
            '--date-x-max',
            '--desc-x-min',
            '--desc-x-max',
            '--deposit-x-min',
            '--deposit-x-max',
            '--withdraw-x-min',
            '--withdraw-x-max',
            '--balance-x-min',
            '--amount-y-tolerance',
        }:
            if i + 1 >= len(args):
                print(f"Error: {arg} requires a numeric value.", file=sys.stderr)
                sys.exit(2)
            try:
                value = parse_float_arg(arg, args[i + 1])
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(2)

            if arg == '--date-x-max':
                date_x_max = value
            elif arg == '--desc-x-min':
                desc_x_min = value
            elif arg == '--desc-x-max':
                desc_x_max = value
            elif arg == '--deposit-x-min':
                deposit_x_min = value
            elif arg == '--deposit-x-max':
                deposit_x_max = value
            elif arg == '--withdraw-x-min':
                withdraw_x_min = value
            elif arg == '--withdraw-x-max':
                withdraw_x_max = value
            elif arg == '--balance-x-min':
                balance_x_min = value
            elif arg == '--amount-y-tolerance':
                amount_y_tolerance = value
            i += 1
        elif arg.startswith('--'):
            print(f"Error: unknown option {arg}", file=sys.stderr)
            sys.exit(2)
        else:
            pdf_path = arg
        i += 1

    if debug:
        debug_blocks(pdf_path, use_tounicode=use_tounicode)
        sys.exit(0)

    data = parse_bank_statement(
        pdf_path,
        date_x_max=date_x_max,
        desc_x_min=desc_x_min,
        desc_x_max=desc_x_max,
        deposit_x_min=deposit_x_min,
        deposit_x_max=deposit_x_max,
        withdraw_x_min=withdraw_x_min,
        withdraw_x_max=withdraw_x_max,
        balance_x_min=balance_x_min,
        amount_y_tolerance=amount_y_tolerance,
        use_tounicode=use_tounicode,
        verbose=verbose,
    )

    if csv_path:
        export_transactions_csv(data, csv_path)
        print(f"CSV exported: {csv_path} ({len(data)} rows)")

    if not data:
        print("No transactions found.")
        print("Tip: run with --debug and adjust X thresholds if needed.")
    else:
        hdr = (f"{'DATE':<12}  {'DESCRIPTION':<55}  "
               f"{'WITHDRAWAL':>12}  {'DEPOSIT':>12}  {'BALANCE':>14}")
        print(hdr)
        print('-' * len(hdr))
        for row in data:
            print(
                f"{row['date']:<12}  "
                f"{row['description'][:15]:<55}  "
                f"{row['withdrawal']:>12}  "
                f"{row['deposit']:>12}  "
                f"{row['balance']:>14}"
            )
        print(f"\nTotal transactions: {len(data)}")
