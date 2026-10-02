#![no_std]
#![no_main]
#![feature(abi_x86_interrupt)]

extern crate alloc;

mod kernel;
mod testing;

#[cfg(not(test))]
use core::panic::PanicInfo;

use crate::{
    kernel::{Kernel, driver::ivshmem},
    testing::{
        harness::{init_cpu, init_dataset, run_test},
        shared_memory::SHARED_MEMORY_MANAGER,
        test_dataset::TestDataset,
    },
};
use alloc::boxed::Box;
use bootloader::{BootInfo, entry_point};
use x86_64::VirtAddr;

entry_point!(kernel_main);

fn kernel_main(boot_info: &'static BootInfo) -> ! {
    Kernel::init(boot_info);

    let start = VirtAddr::new(0x_7777_7777_0000);
    let size = ivshmem::init(start);

    // Enable the present CPU features and publish them in the mailbox.
    let (features, save_mode) = init_cpu();
    {
        let mut shared = SHARED_MEMORY_MANAGER.lock();
        shared.init(start, size, &features, save_mode);
        shared.hello();
    }

    init_dataset(Box::new(TestDataset));

    println!("Starting testing...");

    run_test();
}

pub fn hlt_loop() -> ! {
    loop {
        x86_64::instructions::hlt();
    }
}

/// This function is called on panic.
#[cfg(not(test))]
#[panic_handler]
fn panic(info: &PanicInfo) -> ! {
    println!("{}", info);
    hlt_loop();
}
