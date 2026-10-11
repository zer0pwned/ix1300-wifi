"""Tests for the findings of the third code review.

A: the documents in the output queues count for max_backlog, and unsent jobs
   go into the output queues only while the queues have space.
"""
import time
import unittest

import support  # noqa: F401
import test_delivery
from test_daemon import wait_for
from test_delivery import DeliveryCase, Gate
from ix1300 import jobs as J


class AdmissionTest(DeliveryCase):
    delivering_job = test_delivery.A10DeliveryTest.delivering_job

    def test_queued_deliveries_count_for_max_backlog(self):
        cfg = self.config()
        cfg.service["max_backlog"] = 3
        slow = Gate()
        self.addCleanup(slow.release.set)
        svc = self.service(cfg, {"slow": slow})
        svc.start()
        for n in range(3):
            self.queue_job(svc, n)
        self.assertTrue(wait_for(lambda: svc.backlog == 0 and len(svc.deliveries) == 3, 15))
        self.assertFalse(svc._admit())                    # 3 documents wait for the slow output
        slow.release.set()
        self.assertTrue(wait_for(lambda: svc.jobs_done == 3 and not svc.deliveries, 15))
        self.assertTrue(svc._admit())

    def test_unsent_jobs_go_into_the_queues_lazily(self):
        cfg = self.config()
        cfg.service["max_backlog"] = 3
        slow = Gate()
        self.addCleanup(slow.release.set)
        ids = [self.delivering_job(J.UNSENT, 0, rnd=0) for _ in range(12)]
        svc = self.service(cfg, {"rec": slow})
        svc.retry_first = 0
        svc.retry_period = 0.01
        svc.start()
        self.assertTrue(slow.inside.wait(15))
        time.sleep(0.2)                                   # many examinations of work_dir/unsent
        self.assertLessEqual(len(svc.deliveries), 3)      # at most max_backlog job locks
        slow.release.set()
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [], 20), "not all unsent jobs sent")
        self.assertEqual(sorted(d.job_id for d in slow.docs), sorted(ids))


if __name__ == "__main__":
    unittest.main()
