"""The directory output with keep = "until_sent": a local copy until the upload is complete.

The service deletes the file of the directory output when all other outputs of the
job have the document. If an output fails, the file stays (the safe copy). The
manifest has the path of the file, so a restart or a retry of unsent deletes it too.
"""
import os
import unittest

from test_daemon import CONFIG, RecordOutput, wait_for
from test_delivery import DeliveryCase, Failing, Gate
import support
from ix1300 import config as C, jobs as J, outputs as O


class LocalKeepTest(DeliveryCase):
    def setUp(self):
        super().setUp()
        self.out_dir = os.path.join(self.work, "out")

    def local(self, keep="until_sent"):
        cfg = {"type": "directory", "path": self.out_dir}
        if keep is not None:
            cfg["keep"] = keep
        return O.DirectoryOutput("local", cfg)

    def files(self):
        try:
            return sorted(f for f in os.listdir(self.out_dir) if not f.startswith("."))
        except OSError:
            return []

    def done(self):
        return self.ls(J.JOBS) == [] and self.ls(J.UNSENT) == [] and self.ls(J.FAILED) == []

    def test_success_of_all_outputs_deletes_the_copy(self):
        good = RecordOutput()
        svc = self.service(self.config(), {"local": self.local(), "good": good})
        svc.start()
        self.queue_job(svc)
        self.assertTrue(wait_for(lambda: len(good.docs) == 1 and self.done(), 15), "job not removed")
        self.assertEqual(self.files(), [])

    def test_failed_output_keeps_the_copy(self):
        bad = Failing()
        svc = self.service(self.config(), {"local": self.local(), "bad": bad})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 15), "job not unsent")
        self.assertEqual(len(self.files()), 1)
        st = self.manifest(J.UNSENT, jid)["outputs"]["local"]
        self.assertTrue(st["ok"])
        self.assertEqual(st["file"]["path"], os.path.join(self.out_dir, self.files()[0]))
        self.assertNotIn("file_removed", st)

    def test_later_retry_success_deletes_the_copy(self):
        bad = Failing()
        svc = self.service(self.config(), {"local": self.local(), "bad": bad})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 15), "job not unsent")
        self.assertEqual(len(self.files()), 1)
        bad.ok = True
        svc.clock.advance(3601)                  # the retry thread examines unsent
        self.assertTrue(wait_for(lambda: len(bad.docs) == 1 and self.done(), 15), "no retry")
        self.assertEqual(self.files(), [])

    def test_restart_with_unsent_job_deletes_the_copy(self):
        cfg = self.config()
        bad = Failing()
        svc = self.service(cfg, {"local": self.local(), "bad": bad})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid] and svc.jobs_done == 1, 15))
        svc.stop(5)
        self.assertEqual(len(self.files()), 1)
        good = RecordOutput()
        svc2 = self.service(cfg, {"local": self.local(), "bad": good})
        svc2.retry_first = 0
        svc2.start()
        self.assertTrue(wait_for(lambda: len(good.docs) == 1 and self.done(), 15), "no retry after the restart")
        self.assertEqual(self.files(), [])

    def test_restart_during_delivery_deletes_the_copy(self):
        cfg = self.config()
        slow = Gate()
        self.addCleanup(slow.release.set)
        svc = self.service(cfg, {"local": self.local(), "slow": slow})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(slow.inside.wait(15))
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["local"]["ok"]))
        svc.stop(timeout=1)
        slow.result = False                      # the receiver did not get it
        slow.release.set()
        self.assertTrue(wait_for(lambda: not svc.threads["out-slow"].is_alive(), 10))
        self.assertEqual(len(self.files()), 1)
        local2, slow2 = self.local(), RecordOutput()
        svc2 = self.service(cfg, {"local": local2, "slow": slow2})
        svc2.start()
        self.assertTrue(wait_for(lambda: len(slow2.docs) == 1 and self.done(), 15))
        self.assertEqual(self.files(), [])

    def test_keep_always_keeps_the_file(self):
        for keep in (None, "always"):
            with self.subTest(keep=keep):
                good = RecordOutput()
                svc = self.service(self.config(), {"local": self.local(keep), "good": good})
                svc.start()
                self.queue_job(svc)
                self.assertTrue(wait_for(lambda: len(good.docs) == 1 and self.done(), 15))
                svc.stop(5)
        self.assertEqual(len(self.files()), 2)

    def test_no_other_output_keeps_the_file(self):
        svc = self.service(self.config(), {"local": self.local()})
        svc.start()
        self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.done() and svc.jobs_done == 1, 15))
        self.assertEqual(len(self.files()), 1)

    def test_file_already_gone_is_no_error(self):
        slow = Gate()
        self.addCleanup(slow.release.set)
        svc = self.service(self.config(), {"local": self.local(), "slow": slow})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(slow.inside.wait(15))
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["local"]["ok"]))
        os.remove(os.path.join(self.out_dir, self.files()[0]))     # the user moved it
        slow.release.set()
        self.assertTrue(wait_for(lambda: self.done() and svc.jobs_done == 1, 15))
        self.assertEqual(self.files(), [])

    def test_changed_file_stays(self):
        slow = Gate()
        self.addCleanup(slow.release.set)
        svc = self.service(self.config(), {"local": self.local(), "slow": slow})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(slow.inside.wait(15))
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["local"]["ok"]))
        name = self.files()[0]
        path = os.path.join(self.out_dir, name)
        os.remove(path)
        with open(path, "wb") as fh:                              # a different file with the same name
            fh.write(b"user data")
        slow.release.set()
        self.assertTrue(wait_for(lambda: self.done() and svc.jobs_done == 1, 15))
        self.assertEqual(self.files(), [name])

    def test_path_outside_the_folder_is_not_deleted(self):
        out = self.local()
        other = os.path.join(self.work, "other")
        os.makedirs(other)
        os.makedirs(self.out_dir)
        for path in (os.path.join(other, "a.pdf"), os.path.join(self.out_dir, "..", "other", "a.pdf"),
                     os.path.join(self.out_dir, ".a.pdf")):
            with self.subTest(path=path):
                with open(path, "wb") as fh:
                    fh.write(b"%PDF")
                st = os.stat(path)
                rec = {"path": path, "size": st.st_size, "ino": st.st_ino}
                self.assertFalse(out.remove_copy(rec, "job"))
                self.assertTrue(os.path.exists(path))
        self.assertFalse(out.remove_copy({"path": os.path.join(self.out_dir, "gone.pdf")}, "job"))
        self.assertFalse(out.remove_copy(None, "job"))


OUTPUTS = '''
[outputs.local]
type = "directory"
path = "/data/out"
%s
'''


class LocalKeepConfigTest(unittest.TestCase):
    def load(self, outputs, env=None):
        text = CONFIG % dict(hid=support.HOST_ID, search=9, work="/data") + outputs
        return C.load(text=text, env=env or {})

    def test_keep_values(self):
        for keep in ("always", "until_sent"):
            cfg = self.load(OUTPUTS % ('keep = "%s"' % keep) + '[outputs.hook]\ntype = "webhook"\n'
                            'url = "http://127.0.0.1:9/x"\n')
            self.assertEqual(cfg.outputs["local"]["keep"], keep)
        for bad in ('"sometimes"', "true", "1"):
            with self.assertRaises(C.ConfigError) as cm:
                self.load(OUTPUTS % ("keep = %s" % bad))
            self.assertIn("keep must be", str(cm.exception))

    def test_env_override(self):
        cfg = self.load(OUTPUTS % "", env={"IX1300_OUTPUTS__LOCAL__KEEP": "until_sent"})
        self.assertEqual(cfg.outputs["local"]["keep"], "until_sent")
        with self.assertRaises(C.ConfigError):
            self.load(OUTPUTS % "", env={"IX1300_OUTPUTS__LOCAL__KEEP": "never"})

    def test_warning_without_other_outputs(self):
        with self.assertLogs("ix1300.config", "WARNING") as cm:
            self.load(OUTPUTS % 'keep = "until_sent"')
        self.assertTrue(any("keep = until_sent has no effect without other outputs" in m for m in cm.output))
        # A profile with only the local output gets the warning; the other profiles do not.
        extra = '[outputs.hook]\ntype = "webhook"\nurl = "http://127.0.0.1:9/x"\n'
        text = (CONFIG % dict(hid=support.HOST_ID, search=9, work="/data")).replace(
            "[profiles.receipt]", 'outputs = ["local"]\n\n[profiles.receipt]')
        with self.assertLogs("ix1300.config", "WARNING") as cm:
            C.load(text=text + OUTPUTS % 'keep = "until_sent"' + extra, env={})
        msgs = [m for m in cm.output if "until_sent" in m]
        self.assertEqual(len(msgs), 1)
        self.assertIn("profile doc", msgs[0])

    def test_no_warning_with_other_outputs(self):
        extra = '[outputs.hook]\ntype = "webhook"\nurl = "http://127.0.0.1:9/x"\n'
        with self.assertLogs("ix1300.config", "WARNING") as cm:
            C.load(text=CONFIG % dict(hid=support.HOST_ID, search=9, work="/data") + OUTPUTS % 'keep = "until_sent"'
                   + extra, env={})
            C.log.warning("end")                 # assertNoLogs is not in Python 3.9
        self.assertFalse(any("until_sent" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
