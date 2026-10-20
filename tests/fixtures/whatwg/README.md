# Encoding Standard data from WHATWG

From the [Encoding Standard](https://encoding.spec.whatwg.org/)'s repository, [whatwg/encoding](https://github.com/whatwg/encoding), at commit `be17ac5c49881336538acfc7b40dba3c8009144b` (2026-10-19):

- `encodings.json` is [`encodings.json`](https://github.com/whatwg/encoding/blob/be17ac5c49881336538acfc7b40dba3c8009144b/encodings.json), verbatim: every encoding and its labels. It last changed in commit `02b3036` (2020-05-06).
- `single-byte-indexes.json` holds the 27 single-byte indexes of [`indexes.json`](https://github.com/whatwg/encoding/blob/be17ac5c49881336538acfc7b40dba3c8009144b/indexes.json), the entries whose list has 128 code points, verbatim and in order. Each lists what the bytes 0x80 to 0xFF decode to, `null` for none.

`tests/unit/test_encodings.py` checks that every label resolves to its encoding, and that every single-byte encoding decodes every byte as its index says.

## Licence

The Encoding Standard is © WHATWG (Apple, Google, Mozilla, Microsoft), licensed under the Creative Commons Attribution 4.0 International License. [`LICENSE`](LICENSE) is the repository's licence, copied from the same commit. The label table in `src/web_crawler/encodings.py` is drawn from `encodings.json`; as source code it is under the BSD 3-Clause License, as that licence provides.
