# -*- coding: utf-8 -*-
"""Гистограмма распределения и выраженность процесса (analysis).

Портировано из MR-02m-flasher (ветка `carel`,
tests/test_pid_tuner_avads_robustness.py) без класса TestRobustness: он
проверяет `robustness.py`, который сюда намеренно не портирован — оценка
настройки по нескольким режимам объекта живёт в десктопной утилите, а плата
подбирает под один текущий режим.
"""
from __future__ import annotations

import unittest


from sa02m_flasher.pid.analysis import histogram, response_to_noise_ratio


class TestHistogram(unittest.TestCase):
    def test_counts_sum_to_n(self):
        vals = [1.0, 1.1, 1.2, 2.0, 2.1, 3.0]
        h = histogram(vals, bins=3)
        self.assertEqual(sum(c for _, c in h), len(vals))
        self.assertEqual(len(h), 3)

    def test_degenerate_constant(self):
        h = histogram([5.0, 5.0, 5.0], bins=4)
        self.assertEqual(h, [(5.0, 3)])


class TestResponseRatio(unittest.TestCase):
    def test_pronounced_vs_weak(self):
        import random
        random.seed(3)
        noise = [random.gauss(0, 0.05) for _ in range(80)]
        weak = [10.0 + n for n in noise]                       # только шум
        strong = [10.0 + n + (2.0 if i > 40 else 0.0)          # ступень 2 °C
                  for i, n in enumerate(noise)]
        self.assertLess(response_to_noise_ratio(weak), 4.0)
        self.assertGreater(response_to_noise_ratio(strong), 5.0)


if __name__ == "__main__":
    unittest.main()
