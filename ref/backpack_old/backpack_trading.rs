use crate::types::{
    TradingCommand, OrderInfo, Position, AccountBalance, TradingResult, 
    OrderSide, OrderType, OrderStatus, ExchangeError
};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::broadcast::{Sender, Receiver};
use tokio::time::{sleep, Duration};
use base64::{Engine as _, engine::general_purpose};
use ed25519_dalek::{Signer, Keypair, PublicKey, SecretKey};
use std::collections::BTreeMap;
use tokio_tungstenite::{connect_async, tungstenite::protocol::Message};
use futures_util::{StreamExt, SinkExt};
use url::Url;

/// Backpack API credentials
#[derive(Debug, Clone)]
pub struct BackpackCredentials {
    pub private_key: String,  // Base64 encoded ED25519 private key
    pub public_key: String,   // Base64 encoded ED25519 public key
    pub api_url: String,      // API base URL
}

/// Backpack trading client
pub struct BackpackTradingClient {
    credentials: BackpackCredentials,
    client: Client,
    keypair: Keypair,
}

/// Backpack API response wrapper
#[derive(Debug, Deserialize)]
struct BackpackResponse<T> {
    #[serde(flatten)]
    #[allow(dead_code)]
    data: T,
}

/// Order placement request for Backpack
#[derive(Debug, Serialize)]
struct BackpackOrderRequest {
    symbol: String,
    side: String,
    #[serde(rename = "orderType")]
    order_type: String,
    #[serde(rename = "timeInForce")]
    time_in_force: String,
    quantity: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    price: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(rename = "clientId")]
    client_id: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(rename = "postOnly")]
    post_only: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    #[serde(rename = "reduceOnly")]
    reduce_only: Option<bool>,
}

/// Order response from Backpack
#[derive(Debug, Deserialize)]
struct BackpackOrderResponse {
    id: String,
    #[serde(rename = "clientId")]
    client_id: Option<u32>,
    symbol: String,
    side: String,
    #[serde(rename = "orderType")]
    order_type: String,
    quantity: String,
    price: Option<String>,
    status: String,
    #[serde(rename = "executedQuantity")]
    executed_quantity: String,
    #[serde(rename = "executedQuoteQuantity")]
    executed_quote_quantity: String,
    #[serde(rename = "timeInForce")]
    time_in_force: String,
    #[serde(rename = "createdAt")]
    created_at: u64,
    #[serde(rename = "reduceOnly")]
    reduce_only: Option<bool>,
}

/// Balance information from Backpack (for individual currency)
#[derive(Debug, Deserialize)]
struct BackpackBalanceData {
    available: String,
    locked: String,
    staked: String,
}

/// Balance response from Backpack API (map format)
type BackpackBalanceResponse = std::collections::HashMap<String, BackpackBalanceData>;

/// Position information from Backpack
#[derive(Debug, Deserialize)]
struct BackpackPosition {
    symbol: String,
    side: String,
    size: String,
    #[serde(rename = "notionalValue")]
    #[allow(dead_code)]
    notional_value: String,
    #[serde(rename = "unrealizedPnl")]
    unrealized_pnl: String,
    #[serde(rename = "entryPrice")]
    entry_price: String,
    leverage: String,
    #[serde(rename = "liquidationPrice")]
    #[allow(dead_code)]
    liquidation_price: Option<String>,
}

/// Collateral response from Backpack
#[derive(Debug, Deserialize)]
pub struct BackpackCollateral {
    #[serde(rename = "assetsValue")]
    assets_value: String,
    #[serde(rename = "borrowLiability")]
    #[allow(dead_code)]
    borrow_liability: String,
    #[serde(rename = "marginFraction")]
    margin_fraction: Option<String>,
    #[serde(rename = "netEquity")]
    net_equity: String,
    #[serde(rename = "netEquityAvailable")]
    #[allow(dead_code)]
    net_equity_available: String,
    imf: String,
    mmf: String,
    collateral: Vec<BackpackCollateralAsset>,
}

/// Individual collateral asset
#[derive(Debug, Deserialize)]
pub struct BackpackCollateralAsset {
    symbol: String,
    #[serde(rename = "assetMarkPrice")]
    #[allow(dead_code)]
    asset_mark_price: String,
    #[serde(rename = "totalQuantity")]
    total_quantity: String,
    #[serde(rename = "balanceNotional")]
    #[allow(dead_code)]
    balance_notional: String,
    #[serde(rename = "collateralWeight")]
    #[allow(dead_code)]
    collateral_weight: String,
    #[serde(rename = "collateralValue")]
    #[allow(dead_code)]
    collateral_value: String,
    #[serde(rename = "availableQuantity")]
    available_quantity: String,
}

/// WebSocket subscription message
#[derive(Debug, Serialize)]
struct WebSocketSubscription {
    method: String,
    params: Vec<String>,
}

/// WebSocket authentication message
#[derive(Debug, Serialize)]
struct WebSocketAuth {
    method: String,
    params: WebSocketAuthParams,
}

#[derive(Debug, Serialize)]
struct WebSocketAuthParams {
    #[serde(rename = "apiKey")]
    api_key: String,
    signature: String,
    timestamp: String,
    window: String,
}

/// Order update from WebSocket
#[derive(Debug, Deserialize)]
struct BackpackOrderUpdate {
    #[serde(rename = "id")]
    order_id: String,
    #[serde(rename = "clientId")]
    client_id: Option<String>,
    symbol: String,
    side: String,
    #[serde(rename = "orderType")]
    order_type: String,
    quantity: String,
    price: Option<String>,
    status: String,
    #[serde(rename = "executedQuantity")]
    executed_quantity: String,
    #[serde(rename = "executedPrice")]
    executed_price: Option<String>,
    #[serde(rename = "timestamp")]
    timestamp: u64,
}

/// Position update from WebSocket
#[derive(Debug, Deserialize)]
struct BackpackPositionUpdate {
    symbol: String,
    side: String,
    size: String,
    #[serde(rename = "unrealizedPnl")]
    unrealized_pnl: String,
    #[serde(rename = "entryPrice")]
    entry_price: String,
    leverage: String,
    #[serde(rename = "timestamp")]
    timestamp: u64,
}

/// Cancel order request structure
#[derive(Debug, Serialize)]
struct CancelOrderRequest {
    #[serde(rename = "orderId")]
    order_id: String,
    symbol: String,
}

/// Market info response from Backpack
#[derive(Debug, Deserialize)]
pub struct BackpackMarketInfo {
    pub symbol: String,
    #[serde(rename = "baseSymbol")]
    pub base_symbol: String,
    #[serde(rename = "quoteSymbol")]
    pub quote_symbol: String,
    #[serde(rename = "marketType")]
    pub market_type: String,
    pub filters: MarketFilters,
    #[serde(rename = "fundingInterval")]
    pub funding_interval: Option<i64>,
    #[serde(rename = "fundingRateUpperBound")]
    pub funding_rate_upper_bound: Option<String>,
    #[serde(rename = "fundingRateLowerBound")]
    pub funding_rate_lower_bound: Option<String>,
    #[serde(rename = "openInterestLimit")]
    pub open_interest_limit: Option<String>,
    #[serde(rename = "orderBookState")]
    pub order_book_state: String,
    #[serde(rename = "createdAt")]
    pub created_at: String,
}

#[derive(Debug, Deserialize)]
pub struct MarketFilters {
    pub price: PriceFilter,
    pub quantity: QuantityFilter,
}

#[derive(Debug, Deserialize)]
pub struct PriceFilter {
    #[serde(rename = "minPrice")]
    pub min_price: Option<String>,
    #[serde(rename = "maxPrice")]
    pub max_price: Option<String>,
    #[serde(rename = "tickSize")]
    pub tick_size: String,
}

#[derive(Debug, Deserialize)]
pub struct QuantityFilter {
    #[serde(rename = "minQuantity")]
    pub min_quantity: String,
    #[serde(rename = "maxQuantity")]
    pub max_quantity: Option<String>,
    #[serde(rename = "stepSize")]
    pub step_size: String,
}

impl BackpackTradingClient {
    pub fn new(credentials: BackpackCredentials) -> Result<Self, ExchangeError> {
        // Decode the private key from base64
        let private_key_bytes = general_purpose::STANDARD
            .decode(&credentials.private_key)
            .map_err(|e| ExchangeError::Authentication(format!("Invalid private key format: {}", e)))?;
        
        if private_key_bytes.len() != 32 {
            return Err(ExchangeError::Authentication("Private key must be 32 bytes".to_string()));
        }
        
        let mut key_array = [0u8; 32];
        key_array.copy_from_slice(&private_key_bytes);
        
        let secret_key = SecretKey::from_bytes(&key_array)
            .map_err(|e| ExchangeError::Authentication(format!("Invalid secret key: {}", e)))?;
        let public_key = PublicKey::from(&secret_key);
        let keypair = Keypair { secret: secret_key, public: public_key };
        
        // Verify that the public key matches
        let expected_public_key = general_purpose::STANDARD.encode(keypair.public.to_bytes());
        if expected_public_key != credentials.public_key {
            return Err(ExchangeError::Authentication(
                "Public key doesn't match private key".to_string()
            ));
        }
        
        Ok(Self {
            credentials,
            client: Client::new(),
            keypair,
        })
    }

    /// Generate WebSocket signature for authentication
    fn generate_ws_signature(&self, timestamp: u64, window: u64) -> Result<String, ExchangeError> {
        let message = format!("instruction=subscribe&timestamp={}&window={}", timestamp, window);
        let signature = self.keypair.sign(message.as_bytes());
        Ok(general_purpose::STANDARD.encode(signature.to_bytes()))
    }

    /// Generate Backpack API signature using ED25519
    fn generate_signature(&self, timestamp: u64, window: u64, method: &str, path: &str, body: &str) -> Result<String, ExchangeError> {
        // Determine instruction based on endpoint path
        let instruction = match path {
            path if path.contains("/capital") => {
                if path.contains("/collateral") {
                    "collateralQuery"
                } else {
                    "balanceQuery"
                }
            },
            path if path.contains("/orders") && method == "POST" => "orderExecute",
            path if path.contains("/orders") && method == "GET" => "orderQuery", 
            path if path.contains("/orders") && method == "DELETE" => "orderCancel",
            path if path.contains("/position") => "positionQuery",
            _ => return Err(ExchangeError::Authentication(format!("Unsupported endpoint: {}", path))),
        };
        
        // Create ordered parameters map
        let mut params = BTreeMap::new();
        
        // Add query parameters from URL if present
        if let Some(query_start) = path.find('?') {
            let query = &path[query_start + 1..];
            for param in query.split('&') {
                if let Some((key, value)) = param.split_once('=') {
                    params.insert(key.to_string(), value.to_string());
                }
            }
        }
        
        // Add body parameters for POST requests
        if !body.is_empty() && method == "POST" {
            let json_body: serde_json::Value = serde_json::from_str(body)
                .map_err(|e| ExchangeError::Authentication(format!("Invalid JSON body: {}", e)))?;
            
            // Handle array of orders (batch orders)
            if let Some(array) = json_body.as_array() {
                for (index, item) in array.iter().enumerate() {
                    if let Some(obj) = item.as_object() {
                        for (key, value) in obj {
                            // For single item array, don't add index to key name
                            let param_key = if array.len() > 1 {
                                format!("{}[{}]", key, index)
                            } else {
                                key.clone()
                            };
                            
                            let value_str = match value {
                                serde_json::Value::String(s) => s.clone(),
                                serde_json::Value::Number(n) => n.to_string(),
                                serde_json::Value::Bool(b) => b.to_string(),
                                serde_json::Value::Null => continue,
                                _ => continue,
                            };
                            params.insert(param_key, value_str);
                        }
                    }
                }
            }
            // Handle single object (non-batch)
            else if let Some(obj) = json_body.as_object() {
                for (key, value) in obj {
                    let value_str = match value {
                        serde_json::Value::String(s) => s.clone(),
                        serde_json::Value::Number(n) => n.to_string(),
                        serde_json::Value::Bool(b) => b.to_string(),
                        serde_json::Value::Null => continue, // Skip null values
                        _ => continue, // Skip complex types
                    };
                    params.insert(key.clone(), value_str);
                }
            }
        }
        
        // Build the signing message according to Backpack specification
        let mut message_parts = vec![format!("instruction={}", instruction)];
        
        // Add sorted parameters
        for (key, value) in params.iter() {
            message_parts.push(format!("{}={}", key, value));
        }
        
        // Add timestamp and window at the end
        message_parts.push(format!("timestamp={}", timestamp));
        message_parts.push(format!("window={}", window));
        
        let message = message_parts.join("&");
        
        // Sign the message
        let signature = self.keypair.sign(message.as_bytes());
        
        Ok(general_purpose::STANDARD.encode(signature.to_bytes()))
    }

    /// Get current timestamp in milliseconds
    fn get_timestamp() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64
    }

    /// Make authenticated request to Backpack API
    async fn make_request<T: for<'de> Deserialize<'de>>(
        &self,
        method: &str,
        endpoint: &str,
        body: Option<&str>,
    ) -> Result<T, ExchangeError> {
        let timestamp = Self::get_timestamp();
        let window = 5000u64; // 5 second window
        let path = format!("/api/v1{}", endpoint);
        let body_str = body.unwrap_or("");
        
        let signature = self.generate_signature(timestamp, window, method, &path, body_str)?;
        
        let url = format!("{}{}", self.credentials.api_url, path);
        let mut request_builder = match method {
            "GET" => self.client.get(&url),
            "POST" => self.client.post(&url),
            "DELETE" => self.client.delete(&url),
            _ => return Err(ExchangeError::Trading(format!("Unsupported HTTP method: {}", method))),
        };

        request_builder = request_builder
            .header("X-Timestamp", timestamp.to_string())
            .header("X-Window", window.to_string())
            .header("X-API-Key", &self.credentials.public_key)
            .header("X-Signature", signature)
            .header("Content-Type", "application/json");

        if let Some(body) = body {
            request_builder = request_builder.body(body.to_string());
        }

        let response = request_builder
            .send()
            .await
            .map_err(|e| ExchangeError::RestApi(format!("Request failed: {}", e)))?;

        if !response.status().is_success() {
            let status = response.status();
            let error_text = response.text().await.unwrap_or_default();
            return Err(ExchangeError::RestApi(format!("HTTP error {}: {}", status, error_text)));
        }

        let response_text = response
            .text()
            .await
            .map_err(|e| ExchangeError::RestApi(format!("Failed to read response: {}", e)))?;

        serde_json::from_str(&response_text)
            .map_err(|e| ExchangeError::RestApi(format!("Failed to parse response: {} - Body: {}", e, response_text)))
    }

    /// Convert symbol format to Backpack perpetual contract format
    /// All trading in this system uses perpetual contracts only
    /// BTC/USDT -> BTC_USDC_PERP (Backpack uses USDC and has PERP contracts)
    fn convert_symbol_to_backpack(&self, symbol: &str) -> String {
        let base_symbol = symbol.replace("/", "_");
        // Replace USDT with USDC since Backpack uses USDC, and add _PERP for perpetual contracts
        if base_symbol.ends_with("_USDT") {
            format!("{}_PERP", base_symbol.replace("_USDT", "_USDC"))
        } else {
            format!("{}_PERP", base_symbol)
        }
    }

    /// Convert Backpack symbol format back (BTC_USDC_PERP -> BTC/USDT)
    fn convert_symbol_from_backpack(&self, symbol: &str) -> String {
        let base_symbol = symbol
            .strip_suffix("_PERP")
            .unwrap_or(symbol)
            .replace("_", "/");
        // Convert back USDC to USDT for consistency
        if base_symbol.ends_with("/USDC") {
            base_symbol.replace("/USDC", "/USDT")
        } else {
            base_symbol
        }
    }

    /// Get market information for a symbol from Backpack
    pub async fn get_market_info(&self, symbol: &str) -> Result<BackpackMarketInfo, ExchangeError> {
        let backpack_symbol = self.convert_symbol_to_backpack(symbol);
        
        // Use GET request for market info (no authentication needed)
        let url = format!("{}/api/v1/markets", self.credentials.api_url);
        
        let response = self.client
            .get(&url)
            .send()
            .await
            .map_err(|e| ExchangeError::RestApi(format!("Request failed: {}", e)))?;

        if !response.status().is_success() {
            return Err(ExchangeError::RestApi(format!(
                "HTTP error: {}",
                response.status()
            )));
        }

        let response_text = response
            .text()
            .await
            .map_err(|e| ExchangeError::RestApi(format!("Failed to read response: {}", e)))?;

        // Parse as array of market info
        let markets: Vec<BackpackMarketInfo> = serde_json::from_str(&response_text)
            .map_err(|e| ExchangeError::RestApi(format!("Failed to parse response: {}", e)))?;

        // Find the specific market
        markets
            .into_iter()
            .find(|market| market.symbol == backpack_symbol)
            .ok_or_else(|| ExchangeError::Trading(format!("Market {} not found", backpack_symbol)))
    }

    /// Get minimum quantity for a symbol
    pub async fn get_min_quantity(&self, symbol: &str) -> Result<f64, ExchangeError> {
        let market_info = self.get_market_info(symbol).await?;
        
        market_info.filters.quantity.min_quantity
            .parse::<f64>()
            .map_err(|e| ExchangeError::Trading(format!("Failed to parse min_quantity: {}", e)))
    }

    /// Validate and adjust quantity based on market rules
    pub async fn validate_quantity(&self, symbol: &str, quantity: f64) -> Result<f64, ExchangeError> {
        let market_info = self.get_market_info(symbol).await?;
        
        let min_qty = market_info.filters.quantity.min_quantity
            .parse::<f64>()
            .map_err(|e| ExchangeError::Trading(format!("Failed to parse min_quantity: {}", e)))?;
            
        let step_size = market_info.filters.quantity.step_size
            .parse::<f64>()
            .map_err(|e| ExchangeError::Trading(format!("Failed to parse step_size: {}", e)))?;
            
        // Ensure quantity meets minimum requirement
        let adjusted_qty = quantity.max(min_qty);
        
        // Round to proper step size
        let steps = (adjusted_qty / step_size).round();
        let final_qty = steps * step_size;
        
        if final_qty < min_qty {
            return Ok(min_qty);
        }
        
        Ok(final_qty)
    }

    /// Place an order on Backpack
    pub async fn place_order(&self, command: &TradingCommand) -> Result<TradingResult, ExchangeError> {
        let symbol = self.convert_symbol_to_backpack(&command.symbol);
        
        // Validate and adjust quantity based on market rules
        let validated_quantity = self.validate_quantity(&command.symbol, command.size).await?;
        
        println!("🔄 Backpack Quantity Validation: {} → {} (minimum enforced)", 
            command.size, validated_quantity);
        
        let (order_type, time_in_force, post_only) = match command.order_type {
            OrderType::Market => ("Market".to_string(), "IOC".to_string(), None),
            OrderType::Limit => ("Limit".to_string(), "GTC".to_string(), None),
            OrderType::PostOnly => ("Limit".to_string(), "GTC".to_string(), Some(true)),
            OrderType::FillOrKill => ("Limit".to_string(), "FOK".to_string(), None),
            OrderType::ImmediateOrCancel => ("Limit".to_string(), "IOC".to_string(), None),
        };
        
        let request = BackpackOrderRequest {
            symbol,
            side: match command.side {
                OrderSide::Buy => "Bid".to_string(),
                OrderSide::Sell => "Ask".to_string(),
            },
            order_type,
            time_in_force,
            quantity: validated_quantity.to_string(),
            price: command.price.map(|p| p.to_string()),
            client_id: {
                // Convert command_id string to u32 by using timestamp
                let timestamp = Self::get_timestamp();
                Some((timestamp % u32::MAX as u64) as u32)
            },
            post_only,
            reduce_only: None, // For perpetual contracts, set to None unless specifically reducing position
        };

        // Backpack API expects batch order format (array) even for single orders
        let request_array = vec![request];
        let body = serde_json::to_string(&request_array)
            .map_err(|e| ExchangeError::Trading(format!("Failed to serialize request: {}", e)))?;

        // Response is array format, take first element
        let response_array: Vec<BackpackOrderResponse> = self
            .make_request("POST", "/orders", Some(&body))
            .await?;
        
        let response = response_array.into_iter().next()
            .ok_or_else(|| ExchangeError::Trading("Empty response array".to_string()))?;

        let timestamp = Self::get_timestamp();

        Ok(TradingResult {
            command_id: command.command_id.clone(),
            success: true,
            order_id: Some(response.id),
            error_message: None,
            timestamp,
        })
    }

    /// Get order information
    pub async fn get_order(&self, order_id: &str, symbol: &str) -> Result<Option<OrderInfo>, ExchangeError> {
        let backpack_symbol = self.convert_symbol_to_backpack(symbol);
        let endpoint = format!("/orders?orderId={}&symbol={}", order_id, backpack_symbol);
        
        let order_data: BackpackOrderResponse = self
            .make_request("GET", &endpoint, None)
            .await?;

        let side = match order_data.side.as_str() {
                "Bid" => OrderSide::Buy,
                "Ask" => OrderSide::Sell,
                _ => return Err(ExchangeError::InvalidData(format!("Invalid order side: {}", order_data.side))),
            };

            let order_type = match order_data.order_type.as_str() {
                "Market" => OrderType::Market,
                "Limit" => {
                    if order_data.time_in_force == "GTC" {
                        OrderType::Limit
                    } else if order_data.time_in_force == "FOK" {
                        OrderType::FillOrKill
                    } else {
                        OrderType::ImmediateOrCancel
                    }
                },
                _ => return Err(ExchangeError::InvalidData(format!("Invalid order type: {}", order_data.order_type))),
            };

            let status = match order_data.status.as_str() {
                "New" => OrderStatus::Live,
                "PartiallyFilled" => OrderStatus::PartiallyFilled,
                "Filled" => OrderStatus::Filled,
                "Cancelled" => OrderStatus::Canceled,
                "Pending" => OrderStatus::Live,
                _ => return Err(ExchangeError::InvalidData(format!("Invalid order status: {}", order_data.status))),
            };

        // Calculate average price from executed quote quantity if available
        let avg_price = if order_data.executed_quantity.parse::<f64>().unwrap_or(0.0) > 0.0 {
            let executed_qty = order_data.executed_quantity.parse::<f64>().unwrap_or(0.0);
            let executed_quote = order_data.executed_quote_quantity.parse::<f64>().unwrap_or(0.0);
            if executed_qty > 0.0 && executed_quote > 0.0 {
                Some(executed_quote / executed_qty)
            } else {
                None
            }
        } else {
            None
        };

        Ok(Some(OrderInfo {
            order_id: order_data.id,
            client_order_id: order_data.client_id.map(|id| id.to_string()),
            exchange: "Backpack".to_string(),
            symbol: self.convert_symbol_from_backpack(&order_data.symbol),
            side,
            order_type,
            size: order_data.quantity.parse().unwrap_or(0.0),
            price: order_data.price.as_ref().and_then(|p| p.parse().ok()),
            filled_size: order_data.executed_quantity.parse().unwrap_or(0.0),
            avg_price,
            status,
            created_time: order_data.created_at,
            updated_time: order_data.created_at, // Backpack doesn't provide updated time
        }))
    }

    /// Cancel an order on Backpack
    pub async fn cancel_order(&self, order_id: &str, symbol: &str) -> Result<TradingResult, ExchangeError> {
        let backpack_symbol = self.convert_symbol_to_backpack(symbol);
        
        let request = CancelOrderRequest {
            order_id: order_id.to_string(),
            symbol: backpack_symbol,
        };

        let body = serde_json::to_string(&request)
            .map_err(|e| ExchangeError::Trading(format!("Failed to serialize cancel request: {}", e)))?;

        let _response: BackpackOrderResponse = self
            .make_request("DELETE", "/orders", Some(&body))
            .await?;

        let timestamp = Self::get_timestamp();

        Ok(TradingResult {
            command_id: format!("cancel_{}", order_id),
            success: true,
            order_id: Some(order_id.to_string()),
            error_message: None,
            timestamp,
        })
    }

    /// Get all positions (for futures trading)
    pub async fn get_positions(&self) -> Result<Vec<Position>, ExchangeError> {
        let response: Vec<BackpackPosition> = self
            .make_request("GET", "/position", None)
            .await?;

        let mut positions = Vec::new();
        for pos_data in response {
            // Skip positions with zero size
            if pos_data.size.parse::<f64>().unwrap_or(0.0) == 0.0 {
                continue;
            }

            positions.push(Position {
                exchange: "Backpack".to_string(),
                symbol: self.convert_symbol_from_backpack(&pos_data.symbol),
                side: pos_data.side,
                size: pos_data.size.parse().unwrap_or(0.0),
                avg_price: pos_data.entry_price.parse().unwrap_or(0.0),
                unrealized_pnl: pos_data.unrealized_pnl.parse().unwrap_or(0.0),
                margin: 0.0, // Backpack doesn't provide margin info directly
                leverage: pos_data.leverage.parse().unwrap_or(1.0),
                updated_time: Self::get_timestamp(),
            });
        }

        Ok(positions)
    }

    /// Get collateral information including margin fraction
    pub async fn get_collateral(&self) -> Result<BackpackCollateral, ExchangeError> {
        self.make_request("GET", "/capital/collateral", None).await
    }

    /// Get account balance with margin information
    pub async fn get_account_balance(&self) -> Result<Vec<AccountBalance>, ExchangeError> {
        // Get both balance and collateral data
        let balance_response: BackpackBalanceResponse = self
            .make_request("GET", "/capital", None)
            .await?;

        let collateral_data = self.get_collateral().await.ok();
        let margin_ratio = collateral_data.as_ref()
            .and_then(|c| {
                match &c.margin_fraction {
                    Some(fraction_str) => fraction_str.parse::<f64>().ok(),
                    None => None, // null margin fraction means spot account (no margin trading)
                }
            });

        let mut balances = Vec::new();
        
        // If we have collateral data, add summary balance first
        if let Some(collateral) = &collateral_data {
            let net_equity = collateral.net_equity.parse::<f64>().unwrap_or(0.0);
            
            balances.push(AccountBalance {
                exchange: "Backpack".to_string(),
                currency: "USD".to_string(), // Summary in USD
                total_balance: net_equity,
                available_balance: net_equity, // Use net equity as available
                frozen_balance: 0.0,
                equity: net_equity,
                margin_ratio,
                updated_time: Self::get_timestamp(),
            });
        }

        // Add individual currency balances from the HashMap
        for (symbol, balance_data) in balance_response {
            let available = balance_data.available.parse::<f64>().unwrap_or(0.0);
            let locked = balance_data.locked.parse::<f64>().unwrap_or(0.0);
            let staked = balance_data.staked.parse::<f64>().unwrap_or(0.0);
            let total_balance = available + locked + staked;
            
            // Skip zero balances
            if total_balance == 0.0 {
                continue;
            }

            balances.push(AccountBalance {
                exchange: "Backpack".to_string(),
                currency: symbol,
                total_balance,
                available_balance: available,
                frozen_balance: locked + staked, // Consider both locked and staked as frozen
                equity: total_balance,
                margin_ratio,
                updated_time: Self::get_timestamp(),
            });
        }

        Ok(balances)
    }
}

/// Backpack trading manager that handles commands and provides monitoring data
pub struct BackpackTradingManager {
    client: BackpackTradingClient,
    command_rx: Receiver<TradingCommand>,
    result_tx: Sender<TradingResult>,
    position_tx: Sender<Vec<Position>>,
    balance_tx: Sender<Vec<AccountBalance>>,
    order_update_tx: Option<Sender<OrderInfo>>,
}

impl BackpackTradingManager {
    pub fn new(
        credentials: BackpackCredentials,
        command_rx: Receiver<TradingCommand>,
        result_tx: Sender<TradingResult>,
        position_tx: Sender<Vec<Position>>,
        balance_tx: Sender<Vec<AccountBalance>>,
    ) -> Result<Self, ExchangeError> {
        let client = BackpackTradingClient::new(credentials)?;
        
        Ok(Self {
            client,
            command_rx,
            result_tx,
            position_tx,
            balance_tx,
            order_update_tx: None,
        })
    }

    /// Set order update channel for real-time updates
    pub fn with_order_updates(mut self, order_update_tx: Sender<OrderInfo>) -> Self {
        self.order_update_tx = Some(order_update_tx);
        self
    }

    /// Connect to Backpack WebSocket for real-time updates
    async fn connect_websocket(&self) -> Result<(), ExchangeError> {
        let url = Url::parse("wss://ws.backpack.exchange")
            .map_err(|e| ExchangeError::WebSocket(format!("Invalid WebSocket URL: {}", e)))?;

        let (ws_stream, _) = connect_async(url)
            .await
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to connect to WebSocket: {}", e)))?;

        let (mut ws_sender, mut ws_receiver) = ws_stream.split();

        // Authenticate WebSocket connection
        let timestamp = BackpackTradingClient::get_timestamp();
        let window = 5000u64;
        let signature = self.client.generate_ws_signature(timestamp, window)?;

        let auth_message = WebSocketAuth {
            method: "subscribe".to_string(),
            params: WebSocketAuthParams {
                api_key: self.client.credentials.public_key.clone(),
                signature,
                timestamp: timestamp.to_string(),
                window: window.to_string(),
            },
        };

        let auth_json = serde_json::to_string(&auth_message)
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to serialize auth message: {}", e)))?;

        ws_sender.send(Message::Text(auth_json))
            .await
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to send auth message: {}", e)))?;

        // Subscribe to order updates and position updates for all symbols
        let subscription_message = WebSocketSubscription {
            method: "subscribe".to_string(),
            params: vec![
                "account.orderUpdate.*".to_string(),
                "account.positionUpdate.*".to_string(),
            ],
        };

        let sub_json = serde_json::to_string(&subscription_message)
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to serialize subscription: {}", e)))?;

        ws_sender.send(Message::Text(sub_json))
            .await
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to send subscription: {}", e)))?;

        // Clone channels for the WebSocket handler
        let position_tx = self.position_tx.clone();
        let order_update_tx = self.order_update_tx.clone();

        // Handle incoming WebSocket messages
        tokio::spawn(async move {
            while let Some(message) = ws_receiver.next().await {
                match message {
                    Ok(Message::Text(text)) => {
                        if let Err(e) = Self::handle_ws_message(&text, &position_tx, &order_update_tx).await {
                            eprintln!("Error handling WebSocket message: {:?}", e);
                        }
                    }
                    Ok(Message::Close(_)) => {
                        println!("Backpack WebSocket connection closed");
                        break;
                    }
                    Err(e) => {
                        eprintln!("Backpack WebSocket error: {:?}", e);
                        break;
                    }
                    _ => {} // Ignore other message types
                }
            }
        });

        Ok(())
    }

    /// Handle incoming WebSocket messages
    async fn handle_ws_message(
        text: &str,
        position_tx: &Sender<Vec<Position>>,
        order_update_tx: &Option<Sender<OrderInfo>>,
    ) -> Result<(), ExchangeError> {
        let value: Value = serde_json::from_str(text)
            .map_err(|e| ExchangeError::WebSocket(format!("Failed to parse WebSocket message: {}", e)))?;

        // Check if it's an order update
        if let Some(stream) = value.get("stream").and_then(|s| s.as_str()) {
            if stream.starts_with("account.orderUpdate") {
                if let Some(order_tx) = order_update_tx {
                    if let Ok(order_update) = serde_json::from_value::<BackpackOrderUpdate>(value["data"].clone()) {
                        let order_info = Self::convert_order_update_to_info(order_update)?;
                        if let Err(e) = order_tx.send(order_info) {
                            eprintln!("Failed to send order update: {}", e);
                        }
                    }
                }
            } else if stream.starts_with("account.positionUpdate") {
                if let Ok(position_update) = serde_json::from_value::<BackpackPositionUpdate>(value["data"].clone()) {
                    let position = Self::convert_position_update(position_update);
                    if let Err(e) = position_tx.send(vec![position]) {
                        eprintln!("Failed to send position update: {}", e);
                    }
                }
            }
        }

        Ok(())
    }

    /// Convert order update to OrderInfo
    fn convert_order_update_to_info(update: BackpackOrderUpdate) -> Result<OrderInfo, ExchangeError> {
        let side = match update.side.as_str() {
            "Bid" => OrderSide::Buy,
            "Ask" => OrderSide::Sell,
            _ => return Err(ExchangeError::InvalidData(format!("Invalid order side: {}", update.side))),
        };

        let order_type = match update.order_type.as_str() {
            "Market" => OrderType::Market,
            "Limit" => OrderType::Limit,
            _ => OrderType::Limit, // Default to limit
        };

        let status = match update.status.as_str() {
            "New" => OrderStatus::Live,
            "PartiallyFilled" => OrderStatus::PartiallyFilled,
            "Filled" => OrderStatus::Filled,
            "Cancelled" => OrderStatus::Canceled,
            _ => OrderStatus::Live, // Default to live
        };

        Ok(OrderInfo {
            order_id: update.order_id,
            client_order_id: update.client_id,
            exchange: "Backpack".to_string(),
            symbol: update.symbol.replace("_", "/"),
            side,
            order_type,
            size: update.quantity.parse().unwrap_or(0.0),
            price: update.price.and_then(|p| p.parse().ok()),
            filled_size: update.executed_quantity.parse().unwrap_or(0.0),
            avg_price: update.executed_price.and_then(|p| p.parse().ok()),
            status,
            created_time: update.timestamp,
            updated_time: update.timestamp,
        })
    }

    /// Convert position update to Position
    fn convert_position_update(update: BackpackPositionUpdate) -> Position {
        Position {
            exchange: "Backpack".to_string(),
            symbol: update.symbol.replace("_", "/"),
            side: update.side,
            size: update.size.parse().unwrap_or(0.0),
            avg_price: update.entry_price.parse().unwrap_or(0.0),
            unrealized_pnl: update.unrealized_pnl.parse().unwrap_or(0.0),
            margin: 0.0, // Backpack doesn't provide margin info directly
            leverage: update.leverage.parse().unwrap_or(1.0),
            updated_time: update.timestamp,
        }
    }

    /// Start the trading manager
    pub async fn start(&mut self) {
        println!("Backpack Trading Manager started");

        // Start WebSocket connection for real-time updates
        if let Err(e) = self.connect_websocket().await {
            eprintln!("Failed to connect to Backpack WebSocket: {:?}", e);
            println!("Continuing with REST API polling...");
        } else {
            println!("Backpack WebSocket connected successfully");
        }

        // Spawn command processing task
        let mut command_rx = self.command_rx.resubscribe();
        let result_tx = self.result_tx.clone();
        let client = self.client.clone();
        
        tokio::spawn(async move {
            while let Ok(command) = command_rx.recv().await {
                if command.exchange != "Backpack" {
                    continue;
                }

                println!("Processing Backpack trading command: {:?}", command);
                
                let result = client.place_order(&command).await.unwrap_or_else(|e| {
                    let timestamp = BackpackTradingClient::get_timestamp();
                    
                    TradingResult {
                        command_id: command.command_id.clone(),
                        success: false,
                        order_id: None,
                        error_message: Some(format!("Trading error: {:?}", e)),
                        timestamp,
                    }
                });

                if let Err(e) = result_tx.send(result) {
                    eprintln!("Failed to send trading result: {}", e);
                }
            }
        });

        // Start monitoring loop (as backup to WebSocket)
        loop {
            // Update positions (WebSocket may provide real-time updates, but this serves as backup)
            match self.client.get_positions().await {
                Ok(positions) => {
                    if let Err(e) = self.position_tx.send(positions) {
                        eprintln!("Failed to send positions update: {}", e);
                    }
                }
                Err(e) => {
                    eprintln!("Failed to get Backpack positions: {:?}", e);
                }
            }

            // Update balances (includes margin fraction from collateral endpoint)
            match self.client.get_account_balance().await {
                Ok(balances) => {
                    if let Err(e) = self.balance_tx.send(balances) {
                        eprintln!("Failed to send balance update: {}", e);
                    }
                }
                Err(e) => {
                    eprintln!("Failed to get Backpack account balance: {:?}", e);
                }
            }

            // Wait before next update (longer interval since WebSocket provides real-time updates)
            sleep(Duration::from_secs(10)).await;
        }
    }
}

// Add Clone trait to BackpackTradingClient for async task spawning
impl Clone for BackpackTradingClient {
    fn clone(&self) -> Self {
        // Reconstruct the keypair since ed25519_dalek::Keypair doesn't implement Clone
        let keypair = Keypair {
            secret: SecretKey::from_bytes(&self.keypair.secret.to_bytes()).unwrap(),
            public: self.keypair.public,
        };
        
        Self {
            credentials: self.credentials.clone(),
            client: self.client.clone(),
            keypair,
        }
    }
}