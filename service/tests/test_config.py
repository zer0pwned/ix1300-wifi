import os
import unittest

import support
from ix1300 import config as C
from ix1300 import outputs as O
from ix1300.protocol import AUTO_LENGTH

EXAMPLE = os.path.join(support.ROOT, "config.example.toml")

SAMPLE = '''
# comment
[scanner]
ip = "192.0.2.212"   # inline comment
host_id = "0123456789abcdef"
keepalive_interval = 15.5
renew_after_job = false

[service]
short_press_profile = "doc"
long_press_profile = "receipt"

[outputs.hook]
type = "webhook"
url = 'http://example.invalid/upload'
headers = { "X-Token" = "a\\"b", Accept = "*/*" }

[outputs.paperless]
type = "paperless"
url = "http://paperless:8000"
token = "t"
tags = [1, 2,
        3]

[profiles.doc]
dpi = 300
outputs = ["paperless", "hook"]

[profiles.receipt]
dpi = 200
color = "gray"
duplex = false
length = "auto"
'''


class TomlTest(unittest.TestCase):
    def test_mini_parser(self):
        d = C.mini_toml_loads(SAMPLE)
        self.assertEqual(d["scanner"]["ip"], "192.0.2.212")
        self.assertEqual(d["scanner"]["keepalive_interval"], 15.5)
        self.assertIs(d["scanner"]["renew_after_job"], False)
        self.assertEqual(d["outputs"]["hook"]["headers"], {"X-Token": 'a"b', "Accept": "*/*"})
        self.assertEqual(d["outputs"]["paperless"]["tags"], [1, 2, 3])
        self.assertEqual(d["profiles"]["receipt"]["color"], "gray")

    def test_more_syntax(self):
        d = C.mini_toml_loads('a.b = 0x10\nc = -1_000\n[[x]]\nn = 1\n[[x]]\nn = 2\n[t]\ns = "\\u00e9"\n')
        self.assertEqual(d, {"a": {"b": 16}, "c": -1000, "x": [{"n": 1}, {"n": 2}], "t": {"s": "\u00e9"}})
        with self.assertRaises(C.ConfigError):
            C.mini_toml_loads("a = 1\na = 2\n")
        with self.assertRaises(C.ConfigError):
            C.mini_toml_loads("a = \n")

    def test_example_file_parses(self):
        with open(EXAMPLE) as fh:
            text = fh.read()
        mini = C.mini_toml_loads(text)
        self.assertEqual(mini, C.toml_loads(text))
        cfg = C.load(EXAMPLE, env={})
        self.assertIn("receipt", cfg.profiles)


class ConfigTest(unittest.TestCase):
    def test_load_and_profiles(self):
        cfg = C.load(text=SAMPLE, env={})
        self.assertEqual(cfg.host_id.hex().upper(), "0123456789ABCDEF")
        self.assertEqual(set(cfg.profiles), {"doc", "receipt"})      # the default profile is replaced
        r = cfg.profile("receipt")
        self.assertEqual((r.dpi, r.color, r.duplex, r.length_units), (200, "gray", False, AUTO_LENGTH))
        self.assertEqual(cfg.profile("doc").outputs, ["paperless", "hook"])
        self.assertEqual(cfg.scanner["search_port"], 52217)            # default value
        st = r.scan_settings()
        self.assertEqual((st.dpi, st.color), (200, "gray"))

    def test_env_override(self):
        env = {"IX1300_SCANNER__IP": "10.1.2.3", "IX1300_PROFILES__DOC__DPI": "600",
               "IX1300_OUTPUTS__PAPERLESS__TOKEN": "secret", "IX1300_SCANNER__RENEW_AFTER_JOB": "true",
               "IX1300_OUTPUTS__PAPERLESS__TAGS": "[7]", "OTHER": "x"}
        cfg = C.load(text=SAMPLE, env=env)
        self.assertEqual(cfg.scanner["ip"], "10.1.2.3")
        self.assertEqual(cfg.profile("doc").dpi, 600)
        self.assertEqual(cfg.outputs["paperless"]["token"], "secret")
        self.assertEqual(cfg.outputs["paperless"]["tags"], [7])
        self.assertIs(cfg.scanner["renew_after_job"], True)

    def test_json_config(self):
        cfg = C.load(text='{"scanner": {"host_id": "0011223344556677"}, "profiles": {"a": {"dpi": 150}},'
                          ' "service": {"short_press_profile": "a"}}', env={})
        self.assertEqual(cfg.profile("a").dpi, 150)

    def test_errors(self):
        for text in ('[profiles.default]\ndpi = 250\n',
                     '[profiles.default]\ncolor = "pink"\n',
                     '[profiles.default]\nlength = 2000\n',
                     '[profiles.default]\nunknown = 1\n',
                     '[profiles.default]\noutputs = ["nothing"]\n',
                     '[scanner]\nhost_id = "1234"\n',
                     '[service]\nlong_press_profile = "missing"\n',
                     '[outputs.x]\ntype = "ftp"\n'):
            with self.assertRaises(C.ConfigError, msg=text):
                C.load(text=text, env={})

    def test_length_mm(self):
        cfg = C.load(text='[profiles.default]\nlength = 297\n', env={})
        self.assertEqual(cfg.profile("default").length_units, round(297 / 25.4 * 1200))

    def test_max_sheets_removed(self):
        # The button always scans the whole stack: max_sheets is an unknown key now.
        with self.assertRaises(C.ConfigError) as cm:
            C.load(text='[profiles.default]\nmax_sheets = 1\n', env={})
        self.assertIn("max_sheets", str(cm.exception))
        self.assertFalse(hasattr(C.Profile(name="x"), "max_sheets"))

    def test_retry_max_default(self):
        self.assertEqual(C.load(env={}).scanner["retry_max"], 10.0)
        self.assertEqual(C.load(EXAMPLE, env={}).scanner["retry_max"], 10)


def readme_block(heading):
    """The first ```toml block after HEADING in README.md."""
    with open(os.path.join(support.ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    start = text.index(heading)
    a = text.index("```toml\n", start) + len("```toml\n")
    return text[a:text.index("```", a)]


class EndpointsTest(unittest.TestCase):
    def test_readme_separate_endpoints_example(self):
        # Several outputs of the same type with different names, and one profile for each press.
        cfg = C.load(text=readme_block("Separate outputs for each profile"), env={})
        self.assertEqual({n: o["type"] for n, o in cfg.outputs.items()},
                         {"paperless_docs": "paperless", "paperless_receipts": "paperless",
                          "expense_webhook": "webhook"})
        self.assertEqual(cfg.profile("default").outputs, ["paperless_docs"])
        self.assertEqual(cfg.profile("receipt").outputs, ["paperless_receipts", "expense_webhook"])
        self.assertEqual((cfg.service["short_press_profile"], cfg.service["long_press_profile"]),
                         ("default", "receipt"))
        self.assertEqual(cfg.outputs["paperless_docs"]["tags"], [1])
        self.assertEqual((cfg.outputs["paperless_receipts"]["tags"],
                          cfg.outputs["paperless_receipts"]["document_type"]), ([7], 3))
        built = O.build(cfg.outputs)
        self.assertEqual(sorted(built), ["expense_webhook", "paperless_docs", "paperless_receipts"])
        self.assertIsNot(built["paperless_docs"], built["paperless_receipts"])

    def test_same_type_outputs_env_override(self):
        text = readme_block("Separate outputs for each profile")
        cfg = C.load(text=text, env={"IX1300_OUTPUTS__PAPERLESS_RECEIPTS__TOKEN": "r"})
        self.assertEqual(cfg.outputs["paperless_receipts"]["token"], "r")
        self.assertNotEqual(cfg.outputs["paperless_docs"]["token"], "r")


if __name__ == "__main__":
    unittest.main()
