//! One QEMU/KVM instance running the Aegis kernel, driven through the ivshmem
//! mailbox (see `libaegis::protocol`).

use std::{
    fs::{self, OpenOptions},
    io::{BufRead, BufReader, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use libaegis::{
    protocol::{
        FeatureMask, INIT_MSG, MAILBOX_VERSION, Mailbox, REQUEST_FEATURES_LEN, REQUEST_OFFSET,
        RESULT_OFFSET, STATUS_OK, mask_to_bytes,
    },
    testcase::{TestCase, TestResult},
};
use memmap2::MmapMut;

pub const SHM_SIZE: u64 = 16 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct VmConfig {
    pub qemu: String,
    /// Run the guest under TCG (`-cpu max`) instead of KVM (`-cpu host`).
    pub emulated: bool,
    /// Serial reconnect option: `reconnect=1` (QEMU < 9.2) or `reconnect-ms=1000`.
    pub reconnect_opt: String,
    pub bootimage: PathBuf,
    pub boot_timeout: Duration,
    pub shm_dir: PathBuf,
}

#[derive(Debug)]
pub enum RunError {
    /// The kernel did not acknowledge within the timeout.
    Timeout,
    /// QEMU exited while a case was in flight.
    QemuExited(ExitStatus),
    /// The case does not fit the request area (host side problem, VM is fine).
    Encode,
}

pub struct Reply {
    pub status: u32,
    #[allow(dead_code)]
    pub exception: u32,
    /// Present for status ok / exception.
    pub result: Option<TestResult>,
}

pub struct Vm {
    child: Child,
    shm: MmapMut,
    dir: PathBuf,
    sock_path: PathBuf,
    stop: Arc<AtomicBool>,
    serial: Option<JoinHandle<()>>,
    /// Current serial connection from QEMU (for the HELLO handshake).
    conn: Arc<Mutex<Option<UnixStream>>>,
    /// Set when the guest has answered `HELLO`.
    hello: Arc<AtomicBool>,
    pub features: FeatureMask,
    pub save_mode_raw: u32,
    seq: u64,
}

enum Wait {
    Ready,
    Timeout,
    Exited(ExitStatus),
}

impl Vm {
    pub fn start(cfg: &VmConfig, instance: u64) -> Result<Vm> {
        let dir = cfg
            .shm_dir
            .join(format!("g5k-worker-{}-{}", std::process::id(), instance));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).with_context(|| format!("create {}", dir.display()))?;
        let shm_path = dir.join("shm");
        let sock_path = dir.join("serial.sock");

        // Fresh, zero-filled shared memory file.
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&shm_path)
            .context("create shm file")?;
        file.set_len(SHM_SIZE)?;
        // SAFETY: we own the file; the mapping lives as long as `Vm`.
        let shm = unsafe { MmapMut::map_mut(&file) }.context("mmap shm")?;
        drop(file);

        // Serial server first, QEMU connects (reconnect=1).
        let listener = UnixListener::bind(&sock_path).context("bind serial socket")?;
        let stop = Arc::new(AtomicBool::new(false));
        let conn: Arc<Mutex<Option<UnixStream>>> = Arc::new(Mutex::new(None));
        let hello = Arc::new(AtomicBool::new(false));
        let serial = {
            let (stop, conn, hello) = (stop.clone(), conn.clone(), hello.clone());
            thread::spawn(move || serial_loop(listener, stop, conn, hello))
        };

        let child = Command::new(&cfg.qemu)
            .args(if cfg.emulated { &["-accel", "tcg", "-cpu", "max"][..] } else { &["-enable-kvm", "-cpu", "host"] })
            .arg("-drive")
            // snapshot=on: the image sits on the site's NFS home and is shared by every node
            // there; opened read-write, the first QEMU holds its write lock and the others fail.
            .arg(format!("format=raw,snapshot=on,file={}", cfg.bootimage.display()))
            .arg("-serial")
            .arg(format!("unix:{},{}", sock_path.display(), cfg.reconnect_opt))
            .args(["-device", "isa-debug-exit,iobase=0xf4,iosize=0x04", "-object"])
            .arg(format!(
                "memory-backend-file,id=shm,mem-path={},size=16M,share=on",
                shm_path.display()
            ))
            .args(["-device", "ivshmem-plain,memdev=shm", "-display", "none", "-no-reboot"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .with_context(|| format!("spawn {}", cfg.qemu));
        let child = match child {
            Ok(c) => c,
            Err(e) => {
                stop.store(true, Ordering::SeqCst);
                let _ = UnixStream::connect(&sock_path);
                let _ = serial.join();
                let _ = fs::remove_dir_all(&dir);
                return Err(e);
            }
        };

        let mut vm = Vm {
            child,
            shm,
            dir,
            sock_path,
            stop,
            serial: Some(serial),
            conn,
            hello,
            features: [0; 4],
            save_mode_raw: 0,
            seq: 0,
        };
        // On error `vm` is dropped, which kills QEMU and cleans up.
        vm.await_boot(cfg.boot_timeout)?;
        Ok(vm)
    }

    fn mb(&mut self) -> *mut Mailbox {
        self.shm.as_mut_ptr() as *mut Mailbox
    }

    fn await_boot(&mut self, timeout: Duration) -> Result<()> {
        let deadline = Instant::now() + timeout;
        let p = self.mb();
        // SAFETY: `p` points to the live mapping (at least 4096 bytes).
        match self.wait(deadline, || unsafe { Mailbox::is_ready(p) }) {
            Wait::Ready => {}
            Wait::Timeout => bail!("guest did not publish the mailbox magic within {timeout:?}"),
            Wait::Exited(s) => bail!("QEMU exited before the guest booted: {s}"),
        }
        let version = unsafe { Mailbox::read_version(p) };
        if version != MAILBOX_VERSION {
            bail!("mailbox version {version}, worker expects {MAILBOX_VERSION}");
        }
        self.features = unsafe { Mailbox::read_features(p) };
        self.save_mode_raw = unsafe { Mailbox::read_save_mode(p) };
        self.handshake(deadline)
    }

    /// The kernel still blocks on a serial `HELLO` after publishing the
    /// mailbox. Bytes sent before its UART is initialised can be lost (the
    /// kernel discards malformed lines), so resend `HELLO` until the guest
    /// answers `HELLO`; the reply is the synchronisation point.
    fn handshake(&mut self, deadline: Instant) -> Result<()> {
        let hello = self.hello.clone();
        loop {
            if let Some(c) = self.conn.lock().unwrap().as_mut() {
                let _ = c.write_all(format!("{INIT_MSG}\n").as_bytes());
            }
            let slice = (Instant::now() + Duration::from_millis(250)).min(deadline);
            match self.wait(slice, || hello.load(Ordering::SeqCst)) {
                Wait::Ready => return Ok(()),
                Wait::Exited(s) => bail!("QEMU exited during the serial handshake: {s}"),
                Wait::Timeout if Instant::now() >= deadline => {
                    bail!("guest did not answer HELLO on the serial port")
                }
                Wait::Timeout => {}
            }
        }
    }

    /// Polls `cond` until true, the deadline passes, or QEMU exits. Spins
    /// briefly, then yields, then parks for short intervals so a slow guest
    /// does not pin a core.
    fn wait(&mut self, deadline: Instant, mut cond: impl FnMut() -> bool) -> Wait {
        let mut n: u32 = 0;
        loop {
            if cond() {
                return Wait::Ready;
            }
            n = n.wrapping_add(1);
            if n.is_multiple_of(128) {
                if let Ok(Some(st)) = self.child.try_wait() {
                    // The guest may have acked right before exiting.
                    return if cond() { Wait::Ready } else { Wait::Exited(st) };
                }
                if Instant::now() >= deadline {
                    return Wait::Timeout;
                }
            }
            if n < 2_000 {
                std::hint::spin_loop();
            } else if n < 50_000 {
                thread::yield_now();
            } else {
                thread::park_timeout(Duration::from_micros(100));
            }
        }
    }

    /// Runs one case. `mask` is the required-feature mask sent to the kernel.
    pub fn run_case(
        &mut self,
        mask: &FeatureMask,
        tc: &TestCase,
        timeout: Duration,
    ) -> Result<Reply, RunError> {
        {
            let req = &mut self.shm[REQUEST_OFFSET..RESULT_OFFSET];
            req[..REQUEST_FEATURES_LEN].copy_from_slice(&mask_to_bytes(mask));
            tc.to_bytes(&mut req[REQUEST_FEATURES_LEN..])
                .map_err(|_| RunError::Encode)?;
        }
        let p = self.mb();
        // SAFETY: `p` points to the live mapping; payload is written above.
        let want = unsafe { Mailbox::submit(p) };
        self.seq = want;
        match self.wait(Instant::now() + timeout, || unsafe {
            Mailbox::load_ack_seq(p) == want
        }) {
            Wait::Ready => {}
            Wait::Timeout => return Err(RunError::Timeout),
            Wait::Exited(s) => return Err(RunError::QemuExited(s)),
        }
        let (status, exception) = unsafe { Mailbox::read_status(p) };
        let result = if status == STATUS_OK || status == libaegis::protocol::STATUS_EXCEPTION {
            TestResult::from_bytes(&self.shm[RESULT_OFFSET..]).ok().map(|(_, r)| r)
        } else {
            None
        };
        Ok(Reply { status, exception, result })
    }
}

impl Drop for Vm {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.stop.store(true, Ordering::SeqCst);
        // Wake the blocking accept().
        let _ = UnixStream::connect(&self.sock_path);
        if let Some(h) = self.serial.take() {
            let _ = h.join();
        }
        let _ = fs::remove_dir_all(&self.dir);
    }
}

/// Accepts serial connections (QEMU reconnects) and forwards lines to the log.
fn serial_loop(
    listener: UnixListener,
    stop: Arc<AtomicBool>,
    slot: Arc<Mutex<Option<UnixStream>>>,
    hello: Arc<AtomicBool>,
) {
    while !stop.load(Ordering::SeqCst) {
        let Ok((conn, _)) = listener.accept() else {
            break;
        };
        if stop.load(Ordering::SeqCst) {
            break;
        }
        *slot.lock().unwrap() = conn.try_clone().ok();
        let mut r = BufReader::new(conn);
        let mut buf = Vec::new();
        loop {
            buf.clear();
            match r.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    let line = String::from_utf8_lossy(&buf);
                    let line = line.trim_end();
                    if line == INIT_MSG {
                        hello.store(true, Ordering::SeqCst);
                    }
                    log!("[guest] {line}");
                }
            }
        }
        *slot.lock().unwrap() = None;
    }
}

/// Picks `/dev/shm` when present, else the system temp dir.
pub fn default_shm_dir() -> PathBuf {
    let p = Path::new("/dev/shm");
    if p.is_dir() { p.to_path_buf() } else { std::env::temp_dir() }
}

/// QEMU 9.2 renamed the chardev option `reconnect` (seconds) to `reconnect-ms`.
pub fn detect_reconnect_opt(qemu: &str) -> String {
    let help = Command::new(qemu)
        .args(["-chardev", "socket,help"])
        .stdin(Stdio::null())
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).into_owned() + &String::from_utf8_lossy(&o.stderr))
        .unwrap_or_default();
    if help.contains("reconnect-ms") { "reconnect-ms=1000".into() } else { "reconnect=1".into() }
}

/// Version of the QEMU binary (`7.2.19`), from the first line of `--version`.
pub fn qemu_version(qemu: &str) -> Option<String> {
    let out = Command::new(qemu).arg("--version").stdin(Stdio::null()).output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mut words = text.lines().next()?.split_whitespace();
    words.find(|w| *w == "version")?;
    words.next().map(str::to_string)
}
