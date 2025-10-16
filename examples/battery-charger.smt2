(set-logic QF_LIA)

; === Parameters ===
; Horizon N = 5 (change if you want more steps)
; Minimal positive increment when charging: d_t >= 1
; Initial battery between 0 and 99 (so charging starts automatically)

; --- State variables b_0..b_5 (battery %) and c_0..c_5 (charging?) ---
(declare-const b0 Int) (declare-const b1 Int) (declare-const b2 Int)
(declare-const b3 Int) (declare-const b4 Int) (declare-const b5 Int)
(declare-const c0 Bool) (declare-const c1 Bool) (declare-const c2 Bool)
(declare-const c3 Bool) (declare-const c4 Bool) (declare-const c5 Bool)

; --- Increments d_0..d_4 applied when charging and b<100 ---
(declare-const d0 Int) (declare-const d1 Int) (declare-const d2 Int)
(declare-const d3 Int) (declare-const d4 Int)

; === State bounds & increment assumptions ===
; Battery always a percentage [0..100]
(define-fun InRange ((x Int)) Bool (and (<= 0 x) (<= x 100)))
(assert (InRange b0)) (assert (InRange b1)) (assert (InRange b2))
(assert (InRange b3)) (assert (InRange b4)) (assert (InRange b5))

; Positive progress when charging
(assert (>= d0 1)) (assert (>= d1 1)) (assert (>= d2 1))
(assert (>= d3 1)) (assert (>= d4 1))

; === Initial condition ===
; Start below 100% so charging begins automatically
(assert (and (>= b0 0) (<= b0 99)))

; Charging policy: c_t is determined by b_t
(assert (= c0 (< b0 100)))
(assert (= c1 (< b1 100)))
(assert (= c2 (< b2 100)))
(assert (= c3 (< b3 100)))
(assert (= c4 (< b4 100)))
(assert (= c5 (< b5 100)))

; === Transition relation (t = 0..4) ===
; If b_t < 100 -> we are charging and b_{t+1} = min(100, b_t + d_t)
; If b_t >= 100 -> not charging and b_{t+1} = b_t  (no overcharge)
(define-fun step ((bt Int) (dt Int)) Int
  (ite (< bt 100) (ite (<= (+ bt dt) 100) (+ bt dt) 100) bt))

(assert (= b1 (step b0 d0)))
(assert (= b2 (step b1 d1)))
(assert (= b3 (step b2 d2)))
(assert (= b4 (step b3 d3)))
(assert (= b5 (step b4 d4)))

; === Termination within bound ===
; By step 5, fully charged and charging has auto-stopped
(assert (= b5 100))
(assert (= c5 false))

; === Safety: no overcharge invariant already enforced by InRange and step ===
; (Optional redundancy) assert all b_t <= 100 explicitly
(assert (and (<= b0 100) (<= b1 100) (<= b2 100) (<= b3 100) (<= b4 100) (<= b5 100)))

(check-sat)
(get-model)
