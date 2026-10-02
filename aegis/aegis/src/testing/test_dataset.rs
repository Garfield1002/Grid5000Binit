use libaegis::{
    cpu::CpuState,
    protocol::{
        SAVE_MODE_FXSAVE, STATUS_EXCEPTION, STATUS_OK, STATUS_SKIPPED_MISSING_FEATURE,
        mask_is_subset,
    },
    testcase::{ExceptionInfo, TestCase, TestOutcome, TestResult},
};

use crate::testing::{
    harness::{Dataset, KERNEL_FEATURES, TestId, save_mode},
    shared_memory::SHARED_MEMORY_MANAGER,
};

static mut INITIAL_CPU_STATE: CpuState = CpuState::zero();
/// `req_seq` of the request currently being executed
static mut CURRENT_SEQ: u64 = 0;

/// Test cases come from the host mailbox, one per request.
pub struct TestDataset;

#[allow(static_mut_refs)]
impl Dataset for TestDataset {
    fn next(&self) -> TestCase {
        let features = *KERNEL_FEATURES.lock();
        let mut shared = SHARED_MEMORY_MANAGER.lock();

        loop {
            let req = shared.wait_request();

            if !mask_is_subset(&req.required, &features) {
                // Required features are absent: do not execute, report skipped.
                shared.ack(req.seq, STATUS_SKIPPED_MISSING_FEATURE, 0);
                continue;
            }

            unsafe {
                CURRENT_SEQ = req.seq;
                INITIAL_CPU_STATE = req.test.state.clone();
            }
            return req.test;
        }
    }

    fn after_test(&mut self, id: TestId, state: &CpuState, exception: Option<ExceptionInfo>) {
        let (status, vector) = match &exception {
            Some(e) => (STATUS_EXCEPTION, e.kind as u32),
            None => (STATUS_OK, 0),
        };

        let outcome = match exception {
            Some(exception) => TestOutcome::Exception(exception),
            None => {
                let initial = unsafe { &INITIAL_CPU_STATE };
                let mut diff = state.diff(initial);
                if save_mode() == SAVE_MODE_FXSAVE {
                    // Past the legacy area nothing is captured (state is zero
                    // there). Make the diff equal the initial bytes so that
                    // initial XOR diff reconstructs zeros.
                    diff.avx.data[512..].copy_from_slice(&initial.avx.data[512..]);
                }
                TestOutcome::Completed(diff)
            }
        };

        let res = TestResult { id, outcome };
        let seq = unsafe { CURRENT_SEQ };
        SHARED_MEMORY_MANAGER
            .lock()
            .complete(seq, &res, status, vector);
    }
}
