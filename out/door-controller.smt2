; Generated from requirement set door-controller
; Door Access Controller
(declare-datatypes () ((DoorState (OPEN) (CLOSED))))
(declare-const credential_presented Bool)
(declare-const door_state DoorState)
(declare-const lock_engaged Bool)
(declare-fun next_lock_engaged (DoorState Bool Bool) Bool)
; If the door is closed the controller drives the lock engaged in the next cycle.
; Traceability: FR1
(assert (forall ((state DoorState) (locked Bool) (cred Bool)) (=> (and (= state CLOSED) (= locked false)) (= (next_lock_engaged state locked cred) true))))
; The lock cannot be engaged when the door is open.
; Traceability: FR2
(assert (=> (= door_state OPEN) (= lock_engaged false)))
; A valid credential while closed causes the lock to disengage next cycle.
; Traceability: FR3
(assert (forall ((state DoorState) (locked Bool)) (=> (and (= state CLOSED) credential_presented) (= (next_lock_engaged state locked true) false))))
(check-sat)
(get-model)
