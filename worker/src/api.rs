//! Controller HTTP client (ureq + rustls; honors https_proxy/http_proxy).
//! The `gzip` feature makes ureq send `Accept-Encoding: gzip` and decode responses.

use std::time::Duration;

use anyhow::{Result, anyhow, bail};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::compare::Expected;

#[derive(Debug, Deserialize)]
pub struct Case {
    pub test_case_id: i64,
    pub state_index: i64,
    #[serde(default)]
    pub instruction: Option<String>,
    pub opcode_hex: String,
    #[serde(default)]
    pub required_features: Vec<String>,
    pub initial_state: Map<String, Value>,
    pub expected: Expected,
}

#[derive(Debug, Deserialize)]
pub struct Batch {
    #[serde(default)]
    pub batch_id: Option<i64>,
    #[serde(default)]
    pub cases: Vec<Case>,
}

#[derive(Clone)]
pub struct Api {
    agent: ureq::Agent,
    base: String,
    auth: String,
}

enum Fail {
    /// Worth retrying (network error, 5xx, 408, 429).
    Transient(String),
    /// Retrying will not help (other 4xx, bad body).
    Fatal(String),
}

fn classify(e: ureq::Error) -> Fail {
    match e {
        ureq::Error::StatusCode(c) if c == 408 || c == 429 || c >= 500 => {
            Fail::Transient(format!("HTTP {c}"))
        }
        ureq::Error::StatusCode(c) => Fail::Fatal(format!("HTTP {c}")),
        other => Fail::Transient(other.to_string()),
    }
}

impl Api {
    pub fn new(base: &str, token: &str) -> Api {
        // Proxy settings are read from the environment by default
        // (https_proxy / http_proxy / all_proxy).
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(120)))
            .build()
            .into();
        Api {
            agent,
            base: base.trim_end_matches('/').to_string(),
            auth: format!("Bearer {token}"),
        }
    }

    fn get_once(&self, path: &str, query: &[(&str, String)]) -> Result<Value, Fail> {
        let mut req = self.agent.get(format!("{}{}", self.base, path));
        for (k, v) in query {
            req = req.query(*k, v);
        }
        let mut resp = req.header("Authorization", &self.auth).call().map_err(classify)?;
        resp.body_mut()
            .read_json::<Value>()
            .map_err(|e| Fail::Transient(format!("bad response body: {e}")))
    }

    fn post_once(&self, path: &str, body: &Value) -> Result<Value, Fail> {
        let mut resp = self
            .agent
            .post(format!("{}{}", self.base, path))
            .header("Authorization", &self.auth)
            .send_json(body)
            .map_err(classify)?;
        resp.body_mut()
            .read_json::<Value>()
            .map_err(|e| Fail::Transient(format!("bad response body: {e}")))
    }

    /// Retries transient failures with exponential backoff (1s .. 60s).
    fn retry<T>(&self, what: &str, mut f: impl FnMut() -> Result<T, Fail>) -> Result<T> {
        let mut delay = Duration::from_secs(1);
        loop {
            match f() {
                Ok(v) => return Ok(v),
                Err(Fail::Fatal(m)) => bail!("{what}: {m}"),
                Err(Fail::Transient(m)) => {
                    log!("{what} failed ({m}); retrying in {delay:?}");
                    std::thread::sleep(delay);
                    delay = (delay * 2).min(Duration::from_secs(60));
                }
            }
        }
    }

    pub fn register(&self, body: &Value) -> Result<i64> {
        let v = self.retry("register", || self.post_once("/register", body))?;
        v["node_id"].as_i64().ok_or_else(|| anyhow!("register: no node_id in {v}"))
    }

    pub fn batch(&self, node_id: i64, size: usize) -> Result<Batch> {
        let v = self.retry("batch", || {
            self.get_once("/batch", &[("node_id", node_id.to_string()), ("size", size.to_string())])
        })?;
        Ok(serde_json::from_value(v)?)
    }

    pub fn results(&self, body: &Value) -> Result<()> {
        self.retry("results", || self.post_once("/results", body)).map(|_| ())
    }

    /// Best effort, single attempt.
    pub fn heartbeat(&self, node_id: i64, batch_id: Option<i64>, done: u64, restarts: u64) -> Result<()> {
        let body = json!({"node_id": node_id, "batch_id": batch_id,
                          "done_in_batch": done, "qemu_restarts": restarts});
        self.post_once("/heartbeat", &body).map(|_| ()).map_err(|f| match f {
            Fail::Transient(m) | Fail::Fatal(m) => anyhow!(m),
        })
    }
}
