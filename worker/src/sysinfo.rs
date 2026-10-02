//! Host identification: hostname, cluster, /proc/cpuinfo, CPUID feature names.

use libaegis::protocol::{FeatureBit, FeatureMask, mask_set};
use raw_cpuid::CpuId;

pub fn hostname() -> String {
    for p in ["/proc/sys/kernel/hostname", "/etc/hostname"] {
        if let Ok(s) = std::fs::read_to_string(p) {
            let s = s.trim();
            if !s.is_empty() {
                return s.to_string();
            }
        }
    }
    "unknown".to_string()
}

/// Grid5000 hostnames look like `dahu-12.grenoble.grid5000.fr`; the cluster is
/// the prefix before the first '-'.
pub fn cluster_of(host: &str) -> String {
    let first = host.split('.').next().unwrap_or(host);
    first.split('-').next().unwrap_or(first).to_string()
}

/// First value of `key` in /proc/cpuinfo text.
pub fn cpuinfo_field(text: &str, key: &str) -> Option<String> {
    text.lines().find_map(|l| {
        let (k, v) = l.split_once(':')?;
        (k.trim() == key).then(|| v.trim().to_string())
    })
}

pub fn cpuinfo() -> (Option<String>, Option<String>) {
    let text = std::fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    (cpuinfo_field(&text, "model name"), cpuinfo_field(&text, "microcode"))
}

/// CPU capabilities (CPUID) expressed as libaegis feature bits.
pub fn cpuid_mask() -> FeatureMask {
    let id = CpuId::new();
    let mut m: FeatureMask = [0; 4];
    let mut set = |c: bool, f: FeatureBit| {
        if c {
            mask_set(&mut m, f)
        }
    };
    if let Some(f) = id.get_feature_info() {
        set(f.has_fpu(), FeatureBit::X87);
        set(f.has_mmx(), FeatureBit::MMX);
        set(f.has_sse(), FeatureBit::SSE);
        set(f.has_sse2(), FeatureBit::SSE2);
        set(f.has_sse3(), FeatureBit::SSE3);
        set(f.has_ssse3(), FeatureBit::SSSE3);
        set(f.has_sse41(), FeatureBit::SSE4_1);
        set(f.has_sse42(), FeatureBit::SSE4_2);
        set(f.has_popcnt(), FeatureBit::POPCNT);
        set(f.has_avx(), FeatureBit::AVX);
        set(f.has_fma(), FeatureBit::FMA);
        set(f.has_f16c(), FeatureBit::F16C);
        set(f.has_aesni(), FeatureBit::AES);
        set(f.has_pclmulqdq(), FeatureBit::PCLMULQDQ);
        set(f.has_movbe(), FeatureBit::MOVBE);
        set(f.has_xsave(), FeatureBit::XSAVE);
        set(f.has_oxsave(), FeatureBit::OSXSAVE);
        set(f.has_rdrand(), FeatureBit::RDRAND);
        set(f.has_cmpxchg16b(), FeatureBit::CMPXCHG16B);
    }
    if let Some(f) = id.get_extended_feature_info() {
        set(f.has_bmi1(), FeatureBit::BMI1);
        set(f.has_bmi2(), FeatureBit::BMI2);
        set(f.has_avx2(), FeatureBit::AVX2);
        set(f.has_adx(), FeatureBit::ADX);
        set(f.has_rdseed(), FeatureBit::RDSEED);
        set(f.has_sha(), FeatureBit::SHA);
        set(f.has_avx512f(), FeatureBit::AVX512F);
        set(f.has_avx512bw(), FeatureBit::AVX512BW);
        set(f.has_avx512dq(), FeatureBit::AVX512DQ);
        set(f.has_avx512vl(), FeatureBit::AVX512VL);
        set(f.has_avx512cd(), FeatureBit::AVX512CD);
        set(f.has_avx512vbmi(), FeatureBit::AVX512VBMI);
        set(f.has_avx512vnni(), FeatureBit::AVX512VNNI);
    }
    if let Some(f) = id.get_extended_processor_and_feature_identifiers() {
        set(f.has_lzcnt(), FeatureBit::LZCNT);
    }
    m
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cluster_prefix() {
        assert_eq!(cluster_of("dahu-12.grenoble.grid5000.fr"), "dahu");
        assert_eq!(cluster_of("gros-3-extra"), "gros");
        assert_eq!(cluster_of("laptop"), "laptop");
        assert_eq!(cluster_of("a.b-c.d"), "a");
    }

    #[test]
    fn cpuinfo_parse() {
        let t = "processor\t: 0\nmodel name\t: Intel(R) Xeon(R)\nmicrocode\t: 0xb000040\n";
        assert_eq!(cpuinfo_field(t, "model name").as_deref(), Some("Intel(R) Xeon(R)"));
        assert_eq!(cpuinfo_field(t, "microcode").as_deref(), Some("0xb000040"));
        assert_eq!(cpuinfo_field(t, "nope"), None);
    }
}
