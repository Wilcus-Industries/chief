"""Decoding real macOS ``attributedBody`` blobs (streamtyped) to plain text.

Fixtures are actual blobs pulled from a chat.db, paired with the ground-truth
``message.text`` for the rows that had one — the decoder must reproduce it.
"""

from chief.adapters.imessage_store import decode_attributed_body, text_of

# (attributedBody hex, expected text). The first two rows carried a text
# column we can check against; the third (is_from_me=1) had text NULL and is
# the case the poller previously dropped; the fourth is >127 bytes so it
# exercises the 0x81 two-byte length prefix (a curly apostrophe makes it 144
# UTF-8 bytes).
SHORT = (
    "040b73747265616d747970656481e8038401408484841"
    "24e5341747472696275746564537472696e67008484084"
    "e534f626a656374008592848484084e53537472696e670"
    "19484012b1749e280996c6c2074616b65207468652065"
    "6469"
    "20746f6f868402694901159284848"
    "40c4e5344696374696f6e617279009484016901928496961d5f5f6b494d4d657373"
    "616765506172744174747269627574654e616d658692848484084e534e756d62657"
    "2008484074e5356616c7565009484012a84999900868686",
    "I’ll take the edi too",
)
FROM_ME_ONLY = (
    "040b73747265616d747970656481e803840140848484124e53417474726962757465"
    "64537472696e67008484084e534f626a656374008592848484084e53537472696e67"
    "019484012b105965737373206c6574e280997320676f8684026949010e9284848"
    "40c4e5344696374696f6e617279009484016901928496961d5f5f6b494d4d6573736"
    "1"
    "6765506172744174747269627574654e616d658692848484084e534e756d62657200"
    "8484074e5356616c7565009484012a84999900868686",
    "Yesss let’s go",
)
LONG = (
    "040b73747265616d747970656481e803840140848484194e534d757461626c654174"
    "74726962757465645374"
    "72696e67008484124e5341747472696275746564537472696e67008484084e534f626a656374"
    "0085928484840f4e534d757461626c65537472696e67018484084e53537472696e67"
    "019584012b81900048"
    "6579206d616e2c206170707265636961746520697420616e64207361772074686520"
    "6167656e746963206368616e6e656c21204672656520746f64617920746f20686f70"
    "206f6e20612063616c6c20616e642074616c6b2061626f75742074686520636f6d70"
    "206368616e6e656c3f2049206b6e6f77207768617420736865e2809973206c6f6f6b"
    "696e6720666f7286",
    "Hey man, appreciate it and saw the agentic channel! Free today to hop "
    "on a call and talk about the comp channel? I know what she’s looking for",
)


def test_decodes_short_body_against_ground_truth_text() -> None:
    blob, expected = SHORT
    assert decode_attributed_body(bytes.fromhex(blob)) == expected


def test_decodes_from_me_only_body() -> None:
    blob, expected = FROM_ME_ONLY
    assert decode_attributed_body(bytes.fromhex(blob)) == expected


def test_decodes_long_body_two_byte_length_prefix() -> None:
    blob, expected = LONG
    assert decode_attributed_body(bytes.fromhex(blob)) == expected


def test_text_of_prefers_text_column_when_present() -> None:
    assert text_of("plain text", b"ignored blob") == "plain text"


def test_text_of_falls_back_to_body_when_text_empty() -> None:
    blob = bytes.fromhex(FROM_ME_ONLY[0])
    assert text_of("", blob) == FROM_ME_ONLY[1]
    assert text_of(None, blob) == FROM_ME_ONLY[1]


def test_text_of_empty_when_nothing_usable() -> None:
    assert text_of(None, None) == ""
    assert text_of("", b"no nsstring marker here") == ""
