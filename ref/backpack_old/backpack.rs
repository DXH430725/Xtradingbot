use crate::types::MarketData;
use base64::{Engine as _, engine::general_purpose};
use chrono::DateTime;
use ed25519_dalek::{Keypair, PublicKey, SecretKey, Signer};
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::RwLock;
use tokio::sync::broadcast::Sender;
use tokio::time::{Duration, sleep};
use tokio_tungstenite::{connect_async, tungstenite::protocol::Message};
use url::Url;

#[derive(Clone)]
struct BackpackConfig {
    api_key: String,
    api_secret: String,
}

#[derive(Debug, Deserialize)]
struct Market {
    symbol: String,
    #[serde(rename = "marketType")]
    market_type: String,
    #[serde(rename = "quoteSymbol")]
    quote_symbol: String,
}

#[derive(Debug, Deserialize)]
struct WsMessage<T> {
    stream: String,
    data: T,
}

#[derive(Debug, Deserialize)]
struct MarkPrice {
    e: String,
    #[serde(rename = "E")]
    event_time: u64,
    s: String,
    p: String,
    f: String,
    n: u64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FundingRate {
    #[serde(deserialize_with = "de_str_to_f64")]
    funding_rate: f64,
    interval_end_timestamp: String,
    symbol: String,
}

fn de_str_to_f64<'de, D>(deserializer: D) -> Result<f64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let s: String = Deserialize::deserialize(deserializer)?;
    s.parse::<f64>().map_err(serde::de::Error::custom)
}

fn normalize_backpack_symbol(symbol: &str) -> String {
    symbol.replace("_", "/")
}

fn calc_latency(prev_latency: &mut HashMap<String, u64>, symbol: &str, new_latency: u64) -> u64 {
    let avg = prev_latency.get(symbol).copied().unwrap_or(new_latency);
    let smooth = (avg * 3 + new_latency) / 4;
    prev_latency.insert(symbol.to_string(), smooth);
    smooth
}

fn generate_signature(
    config: &BackpackConfig,
    instruction: &str,
    params: &str,
) -> (String, String) {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    let window = 5000;

    let signing_string = format!(
        "instruction={}&{}&timestamp={}&window={}",
        instruction, params, timestamp, window
    );

    let secret_bytes = general_purpose::STANDARD
        .decode(&config.api_secret)
        .expect("Invalid API secret format");

    let keypair = if secret_bytes.len() == 32 {
        let secret = SecretKey::from_bytes(&secret_bytes).expect("Invalid private key");
        let public = PublicKey::from(&secret);
        Keypair { secret, public }
    } else {
        panic!(
            "Invalid key length: expected 32 bytes, got {}",
            secret_bytes.len()
        );
    };

    let signature = keypair.sign(signing_string.as_bytes());
    let signature_b64 = general_purpose::STANDARD.encode(signature.to_bytes());

    (timestamp.to_string(), signature_b64)
}

async fn fetch_markets(client: &Client, config: &BackpackConfig) -> Vec<String> {
    let url = "https://api.backpack.exchange/api/v1/markets";

    let (timestamp, signature) = generate_signature(config, "marketsQuery", "");

    let response = client
        .get(url)
        .header("X-API-Key", &config.api_key)
        .header("X-Timestamp", &timestamp)
        .header("X-Window", "5000")
        .header("X-Signature", &signature)
        .send()
        .await;

    match response {
        Ok(resp) => {
            if !resp.status().is_success() {
                let status = resp.status();
                let body = resp.text().await.unwrap_or_default();
                eprintln!(
                    "Failed to fetch Backpack markets: status {}, body: {}",
                    status, body
                );
                return vec![];
            }
            let body = resp.text().await.unwrap_or_default();
            let markets: Vec<Market> = match serde_json::from_str(&body) {
                Ok(markets) => markets,
                Err(e) => {
                    eprintln!(
                        "Failed to parse Backpack markets: {}, raw response: {}",
                        e, body
                    );
                    return vec![];
                }
            };

            let symbols = markets
                .into_iter()
                .filter(|m| {
                    m.market_type == "PERP"
                        && (m.quote_symbol == "USDT" || m.quote_symbol == "USDC")
                })
                .map(|m| m.symbol)
                .collect::<Vec<String>>();

            if symbols.is_empty() {
                eprintln!("No USDT perp markets found in Backpack response");
            } else {
                println!("Backpack: Found {} USDT perp markets", symbols.len());
            }
            symbols
        }
        Err(e) => {
            eprintln!("Failed to fetch Backpack markets: {}", e);
            vec![]
        }
    }
}

async fn fetch_funding_rates(
    client: &Client,
    config: &BackpackConfig,
    symbol: &str,
) -> Option<f64> {
    let url = format!(
        "https://api.backpack.exchange/api/v1/fundingRates?symbol={}&limit=2",
        symbol
    );
    let (timestamp, signature) = generate_signature(
        config,
        "fundingRatesQuery",
        &format!("symbol={}&limit=2", symbol),
    );

    let response = client
        .get(&url)
        .header("X-API-Key", &config.api_key)
        .header("X-Timestamp", &timestamp)
        .header("X-Window", "5000")
        .header("X-Signature", &signature)
        .send()
        .await;

    match response {
        Ok(resp) => {
            if !resp.status().is_success() {
                eprintln!(
                    "Failed to fetch funding rates for {}: status {}",
                    symbol,
                    resp.status()
                );
                return None;
            }

            let text = match resp.text().await {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("Failed to read response body for {}: {}", symbol, e);
                    return None;
                }
            };

            let rates: Vec<FundingRate> = match serde_json::from_str(&text) {
                Ok(rates) => rates,
                Err(e) => {
                    eprintln!("Failed to parse funding rates for {}: {}", symbol, e);
                    return None;
                }
            };

            if rates.len() < 2 {
                eprintln!("Insufficient funding rate data for {}", symbol);
                return None;
            }

            // 解析 ISO8601 时间为毫秒
            let t0 = DateTime::parse_from_rfc3339(&rates[0].interval_end_timestamp)
                .ok()?
                .timestamp_millis();
            let t1 = DateTime::parse_from_rfc3339(&rates[1].interval_end_timestamp)
                .ok()?
                .timestamp_millis();

            let interval_hours = (t0 - t1) as f64 / 3_600_000.0;
            Some(interval_hours)
        }
        Err(e) => {
            eprintln!("Failed to fetch funding rates for {}: {}", symbol, e);
            None
        }
    }
}

async fn periodic_funding_interval_update(
    client: Client,
    config: BackpackConfig,
    symbols: Vec<String>,
    funding_intervals: Arc<RwLock<HashMap<String, f64>>>,
) {
    loop {
        for symbol in &symbols {
            if let Some(interval) = fetch_funding_rates(&client, &config, symbol).await {
                funding_intervals
                    .write()
                    .await
                    .insert(symbol.clone(), interval);
            }
        }
        println!(
            "Backpack: Updated funding intervals for {} symbols",
            symbols.len()
        );
        sleep(Duration::from_secs(600)).await;
    }
}

async fn handle_backpack_ws(
    symbols: Vec<String>,
    tx: Sender<MarketData>,
    _config: BackpackConfig,
    funding_intervals: Arc<RwLock<HashMap<String, f64>>>,
) {
    let ws_url = "wss://ws.backpack.exchange";

    let mut latency_map: HashMap<String, u64> = HashMap::new();
    let mut funding_rate_map: HashMap<String, f64> = HashMap::new();

    loop {
        let (ws_stream, _) = match connect_async(Url::parse(ws_url).unwrap()).await {
            Ok(stream) => stream,
            Err(e) => {
                eprintln!(
                    "Failed to connect to Backpack WebSocket: {}, retrying in 5 seconds",
                    e
                );
                sleep(Duration::from_secs(5)).await;
                continue;
            }
        };

        let (mut write, mut read) = ws_stream.split();

        let params: Vec<String> = symbols.iter().map(|s| format!("markPrice.{}", s)).collect();

        let subscription = serde_json::json!({
            "method": "SUBSCRIBE",
            "params": params
        });

        if let Err(e) = write
            .send(Message::Text(serde_json::to_string(&subscription).unwrap()))
            .await
        {
            eprintln!("Failed to send Backpack subscription: {}", e);
            sleep(Duration::from_secs(5)).await;
            continue;
        }

        println!("Backpack: WS subscribed to {} symbols", symbols.len());

        while let Some(message) = read.next().await {
            match message {
                Ok(Message::Ping(_)) => {
                    let _ = write.send(Message::Pong(vec![])).await;
                }
                Ok(Message::Text(text)) => {
                    if let Ok(wrapper) = serde_json::from_str::<WsMessage<MarkPrice>>(&text) {
                        let msg = wrapper.data;
                        if msg.e == "markPrice" {
                            let display_symbol =
                                normalize_backpack_symbol(&msg.s.replace("_USDC", "_USDT"));
                            let display_symbol =
                                display_symbol.trim_end_matches("/PERP").to_string();

                            let price = msg.p.parse::<f64>().unwrap_or(0.0);
                            let funding_rate = msg.f.parse::<f64>().unwrap_or(0.0);
                            funding_rate_map.insert(msg.s.clone(), funding_rate);

                            let funding_rate_frequency = {
                                funding_intervals
                                    .read()
                                    .await
                                    .get(&msg.s)
                                    .copied()
                                    .unwrap_or(8.0)
                            };

                            let local_time = SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .unwrap()
                                .as_millis() as u64;
                            let latency = calc_latency(
                                &mut latency_map,
                                &msg.s,
                                local_time.saturating_sub(msg.event_time / 1000),
                            );

                            let market_data = MarketData {
                                exchange: "Backpack".to_string(),
                                symbol: display_symbol,
                                price,
                                funding_rate,
                                funding_rate_frequency,
                                timestamp: msg.event_time / 1000,
                                latency,
                            };
                            let _ = tx.send(market_data);
                        }
                    } else {
                        eprintln!("Backpack WS raw: {}", text);
                    }
                }
                Ok(_) => {}
                Err(e) => {
                    eprintln!("Backpack WebSocket error: {}, reconnecting in 5 seconds", e);
                    break;
                }
            }
        }

        eprintln!("Backpack WebSocket disconnected, reconnecting in 5 seconds");
        sleep(Duration::from_secs(5)).await;
    }
}

pub async fn start_backpack_data(tx: Sender<MarketData>, api_key: String, api_secret: String) {
    let config = BackpackConfig {
        api_key,
        api_secret,
    };

    let client = Client::new();
    let symbols = fetch_markets(&client, &config).await;

    if symbols.is_empty() {
        eprintln!("No Backpack USDT spot markets found, skipping WebSocket subscription.");
        return;
    }

    println!(
        "Backpack: Subscribing to {} Backpack USDT spot markets",
        symbols.len()
    );

    let funding_intervals = Arc::new(RwLock::new(HashMap::new()));

    let client_clone = client.clone();
    let config_clone = config.clone();
    let symbols_clone = symbols.clone();
    let funding_intervals_clone = funding_intervals.clone(); // Clone Arc here
    tokio::spawn(async move {
        periodic_funding_interval_update(
            client_clone,
            config_clone,
            symbols_clone,
            funding_intervals_clone,
        )
        .await;
    });

    let chunked: Vec<Vec<String>> = symbols.chunks(200).map(|c| c.to_vec()).collect();

    for chunk in chunked {
        let tx_clone = tx.clone();
        let config_clone = config.clone();
        let funding_intervals_clone = funding_intervals.clone(); // Clone Arc here
        tokio::spawn(async move {
            handle_backpack_ws(chunk, tx_clone, config_clone, funding_intervals_clone).await;
        });
    }
}
