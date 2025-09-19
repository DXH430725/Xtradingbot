import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from bidict import bidict

import hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants as CONSTANTS
from hummingbot.connector.derivative.backpack_perpetual import (
    backpack_perpetual_utils,
    backpack_perpetual_web_utils as web_utils,
)
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_api_order_book_data_source import (
    BackpackPerpetualAPIOrderBookDataSource,
)
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_auth import BackpackPerpetualAuth
from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_user_stream_data_source import (
    BackpackPerpetualUserStreamDataSource,
)
from hummingbot.connector.derivative.position import Position
from hummingbot.connector.perpetual_derivative_py_base import PerpetualDerivativePyBase
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.connector.utils import combine_to_hb_trading_pair, split_hb_trading_pair
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionMode, PositionSide, TradeType
from hummingbot.core.data_type.in_flight_order import InFlightOrder, OrderState, OrderUpdate, TradeUpdate
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.trade_fee import TokenAmount, TradeFeeBase, TradeFeeSchema
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.estimate_fee import build_trade_fee
from hummingbot.core.web_assistant.connections.data_types import RESTMethod
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory

if TYPE_CHECKING:
    from hummingbot.client.config.config_helpers import ClientConfigAdapter

s_decimal_NaN = Decimal("nan")
s_decimal_0 = Decimal(0)


class BackpackPerpetualDerivative(PerpetualDerivativePyBase):
    """
    Backpack Perpetual derivative trading connector
    """

    web_utils = web_utils

    def __init__(
        self,
        client_config_map: "ClientConfigAdapter",
        backpack_perpetual_private_key: str,
        backpack_perpetual_public_key: str,
        trading_pairs: Optional[List[str]] = None,
        trading_required: bool = True,
        domain: str = CONSTANTS.DEFAULT_DOMAIN,
    ):
        """
        Initialize the Backpack Perpetual derivative connector
        
        :param client_config_map: Client configuration map
        :param backpack_perpetual_private_key: Base64 encoded ED25519 private key
        :param backpack_perpetual_public_key: Base64 encoded ED25519 public key
        :param trading_pairs: List of trading pairs to track
        :param trading_required: Whether trading is required
        :param domain: Domain identifier
        """
        self.backpack_perpetual_private_key = backpack_perpetual_private_key
        self.backpack_perpetual_public_key = backpack_perpetual_public_key
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._domain = domain
        self._last_trade_history_timestamp = None

        super().__init__(client_config_map)

    @property
    def name(self) -> str:
        return CONSTANTS.EXCHANGE_NAME

    @property
    def authenticator(self) -> BackpackPerpetualAuth:
        return BackpackPerpetualAuth(
            private_key=self.backpack_perpetual_private_key,
            public_key=self.backpack_perpetual_public_key,
            time_provider=self._time_synchronizer
        )

    @property
    def rate_limits_rules(self) -> List[RateLimit]:
        return CONSTANTS.RATE_LIMITS

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def client_order_id_max_length(self) -> int:
        return CONSTANTS.MAX_ORDER_ID_LEN

    @property
    def client_order_id_prefix(self) -> str:
        return CONSTANTS.HBOT_ORDER_ID_PREFIX

    @property
    def trading_rules_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH_URL

    @property
    def trading_pairs_request_path(self) -> str:
        return CONSTANTS.MARKETS_PATH_URL

    @property
    def check_network_request_path(self) -> str:
        return CONSTANTS.SYSTEM_STATUS_PATH_URL

    @property
    def trading_pairs(self):
        return self._trading_pairs

    @property
    def is_cancel_request_in_exchange_synchronous(self) -> bool:
        return False

    @property
    def is_trading_required(self) -> bool:
        return self._trading_required

    @property
    def funding_fee_poll_interval(self) -> int:
        return 120

    def supported_order_types(self) -> List[OrderType]:
        """
        Return list of supported order types
        """
        return [OrderType.LIMIT, OrderType.MARKET]

    def supported_position_modes(self) -> List[PositionMode]:
        """
        Return list of supported position modes
        """
        return [PositionMode.ONEWAY, PositionMode.HEDGE]

    def get_buy_collateral_token(self, trading_pair: str) -> str:
        """
        Get the collateral token for buy orders
        """
        trading_rule: TradingRule = self._trading_rules.get(trading_pair)
        if trading_rule is None:
            # Default to USDC for most perpetual contracts on Backpack
            return "USDC"
        return trading_rule.buy_order_collateral_token

    def get_sell_collateral_token(self, trading_pair: str) -> str:
        """
        Get the collateral token for sell orders
        """
        return self.get_buy_collateral_token(trading_pair)

    def _is_request_exception_related_to_time_synchronizer(self, request_exception: Exception):
        """
        Check if request exception is related to time synchronization
        """
        error_description = str(request_exception)
        return "timestamp" in error_description.lower() or "time" in error_description.lower()

    def _is_order_not_found_during_status_update_error(self, status_update_exception: Exception) -> bool:
        """
        Check if the exception indicates order not found during status update
        """
        return "order not found" in str(status_update_exception).lower()

    def _is_order_not_found_during_cancelation_error(self, cancelation_exception: Exception) -> bool:
        """
        Check if the exception indicates order not found during cancellation
        """
        return "order not found" in str(cancelation_exception).lower()

    def _create_web_assistants_factory(self) -> WebAssistantsFactory:
        """
        Create web assistants factory for API requests
        """
        return web_utils.build_api_factory(
            throttler=self._throttler,
            time_synchronizer=self._time_synchronizer,
            domain=self._domain,
            auth=self._auth,
        )

    def _create_order_book_data_source(self) -> OrderBookTrackerDataSource:
        """
        Create order book data source
        """
        return BackpackPerpetualAPIOrderBookDataSource(
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _create_user_stream_data_source(self) -> UserStreamTrackerDataSource:
        """
        Create user stream data source
        """
        return BackpackPerpetualUserStreamDataSource(
            auth=self._auth,
            trading_pairs=self._trading_pairs,
            connector=self,
            api_factory=self._web_assistants_factory,
            domain=self.domain,
        )

    def _get_fee(self,
                 base_currency: str,
                 quote_currency: str,
                 order_type: OrderType,
                 order_side: TradeType,
                 amount: Decimal,
                 price: Decimal = s_decimal_NaN,
                 is_maker: Optional[bool] = None) -> TradeFeeBase:
        """
        Calculate trading fees for an order
        """
        trading_fee_schema = backpack_perpetual_utils.get_trading_fees()
        if is_maker:
            fee_percent = trading_fee_schema.maker_percent_fee_decimal
        else:
            fee_percent = trading_fee_schema.taker_percent_fee_decimal

        fee = build_trade_fee(
            exchange=self.name,
            is_maker=is_maker,
            order_side=order_side,
            order_type=order_type,
            amount=amount,
            price=price,
            fee_percent=fee_percent
        )
        return fee

    async def _place_order(self,
                          order_id: str,
                          trading_pair: str,
                          amount: Decimal,
                          trade_type: TradeType,
                          order_type: OrderType,
                          price: Decimal,
                          position_action: PositionAction = PositionAction.OPEN,
                          **kwargs) -> Tuple[str, float]:
        """
        Place an order on the exchange
        """
        symbol = await self.exchange_symbol_associated_to_pair(trading_pair)
        api_params = {
            "symbol": symbol,
            "side": "Buy" if trade_type is TradeType.BUY else "Sell",
            "orderType": CONSTANTS.ORDER_TYPE_MAP[order_type],
            "quantity": str(amount),
            "timeInForce": CONSTANTS.DEFAULT_TIME_IN_FORCE,
        }

        if order_type == OrderType.LIMIT:
            api_params["price"] = str(price)

        if position_action == PositionAction.CLOSE:
            api_params["reduceOnly"] = True

        order_result = await self._api_post(
            path_url=CONSTANTS.ORDER_PATH_URL,
            data=api_params,
            is_auth_required=True,
            trading_pair=trading_pair,
        )

        exchange_order_id = order_result["id"]
        return exchange_order_id, self.current_timestamp

    async def _place_cancel(self, order_id: str, tracked_order: InFlightOrder):
        """
        Cancel an order on the exchange
        """
        symbol = await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair)
        api_params = {
            "symbol": symbol,
            "orderId": tracked_order.exchange_order_id,
        }

        cancel_result = await self._api_delete(
            path_url=CONSTANTS.ORDER_PATH_URL,
            params=api_params,
            is_auth_required=True,
        )

        return cancel_result.get("orderId") == tracked_order.exchange_order_id

    async def _format_trading_rules(self, exchange_info_dict: Dict[str, Any]) -> List[TradingRule]:
        """
        Format trading rules from exchange info
        """
        trading_rules = []
        if "symbols" in exchange_info_dict:
            for symbol_info in exchange_info_dict["symbols"]:
                try:
                    trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol_info["symbol"])
                    
                    min_order_size = Decimal(symbol_info.get("minOrderSize", "0.01"))
                    max_order_size = Decimal(symbol_info.get("maxOrderSize", "1000000"))
                    min_price_increment = Decimal(symbol_info.get("tickSize", "0.01"))
                    min_base_amount_increment = Decimal(symbol_info.get("stepSize", "0.01"))
                    min_notional_size = Decimal(symbol_info.get("minNotional", "10"))

                    trading_rule = TradingRule(
                        trading_pair=trading_pair,
                        min_order_size=min_order_size,
                        max_order_size=max_order_size,
                        min_price_increment=min_price_increment,
                        min_base_amount_increment=min_base_amount_increment,
                        min_notional_size=min_notional_size,
                    )

                    trading_rules.append(trading_rule)
                except Exception as e:
                    self.logger().error(f"Error parsing trading rule for {symbol_info}: {e}", exc_info=True)

        return trading_rules

    async def _update_trading_fees(self):
        """
        Update trading fees (Backpack may not have dynamic fees endpoint)
        """
        pass

    async def _user_stream_event_listener(self):
        """
        Listen to user stream events and process them
        """
        async for stream_message in self._iter_user_event_queue():
            try:
                event_type = stream_message.get("stream", "")
                event_data = stream_message.get("data", {})

                if event_type == CONSTANTS.WS_ORDER_UPDATE_TOPIC:
                    self._process_order_event(event_data)
                elif event_type == CONSTANTS.WS_POSITION_UPDATE_TOPIC:
                    await self._process_account_position_event(event_data)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(f"Unexpected error in user stream listener loop: {e}", exc_info=True)
                await self._sleep(5.0)

    def _process_order_event(self, order_data: Dict[str, Any]):
        """
        Process order update events from WebSocket
        """
        exchange_order_id = order_data.get("id")
        client_order_id = order_data.get("clientId")
        
        if not exchange_order_id and not client_order_id:
            return

        tracked_order = None
        if client_order_id:
            tracked_order = self._order_tracker.fetch_order(client_order_id=client_order_id)
        elif exchange_order_id:
            tracked_order = self._order_tracker.fetch_tracked_order(exchange_order_id=exchange_order_id)

        if tracked_order is None:
            return

        order_status = order_data.get("status", "")
        new_state = CONSTANTS.ORDER_STATE_MAP.get(order_status, OrderState.OPEN)

        order_update = OrderUpdate(
            trading_pair=tracked_order.trading_pair,
            update_timestamp=self.current_timestamp,
            new_state=new_state,
            client_order_id=tracked_order.client_order_id,
            exchange_order_id=exchange_order_id,
        )

        self._order_tracker.process_order_update(order_update)

        # Process trade update if order was filled
        if order_status in ["Filled", "PartiallyFilled"]:
            executed_amount = Decimal(order_data.get("executedQuantity", "0"))
            executed_price = Decimal(order_data.get("price", "0"))
            
            if executed_amount > 0:
                trade_update = TradeUpdate(
                    trading_pair=tracked_order.trading_pair,
                    fill_timestamp=self.current_timestamp,
                    fill_price=executed_price,
                    fill_base_amount=executed_amount,
                    fill_quote_amount=executed_amount * executed_price,
                    fee=TokenAmount(
                        amount=Decimal("0"),  # Fee calculation would need API data
                        token="USDC"
                    ),
                    trade_id=order_data.get("tradeId", ""),
                )
                self._order_tracker.process_trade_update(trade_update)

    async def _process_account_position_event(self, position_data: Dict[str, Any]):
        """
        Process position update events from WebSocket
        """
        try:
            symbol = position_data.get("symbol", "")
            trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol)
            
            position_size = Decimal(position_data.get("size", "0"))
            entry_price = Decimal(position_data.get("entryPrice", "0"))
            unrealized_pnl = Decimal(position_data.get("unrealizedPnl", "0"))

            position_side = PositionSide.LONG if position_size > 0 else PositionSide.SHORT
            abs_position_size = abs(position_size)

            position = Position(
                trading_pair=trading_pair,
                position_side=position_side,
                unrealized_pnl=unrealized_pnl,
                entry_price=entry_price,
                amount=abs_position_size,
            )

            self._account_positions[trading_pair] = position

        except Exception as e:
            self.logger().error(f"Error processing position event: {e}", exc_info=True)

    async def _all_trade_updates_for_order(self, order: InFlightOrder) -> List[TradeUpdate]:
        """
        Get all trade updates for a specific order
        """
        trade_updates = []
        try:
            symbol = await self.exchange_symbol_associated_to_pair(order.trading_pair)
            params = {
                "symbol": symbol,
                "orderId": order.exchange_order_id,
            }

            fills_response = await self._api_get(
                path_url=CONSTANTS.FILLS_PATH_URL,
                params=params,
                is_auth_required=True,
            )

            for fill in fills_response.get("fills", []):
                trade_update = TradeUpdate(
                    trading_pair=order.trading_pair,
                    fill_timestamp=int(fill.get("timestamp", 0)) / 1000,
                    fill_price=Decimal(fill.get("price", "0")),
                    fill_base_amount=Decimal(fill.get("quantity", "0")),
                    fill_quote_amount=Decimal(fill.get("quoteQuantity", "0")),
                    fee=TokenAmount(
                        amount=Decimal(fill.get("fee", "0")),
                        token=fill.get("feeAsset", "USDC")
                    ),
                    trade_id=fill.get("id", ""),
                )
                trade_updates.append(trade_update)

        except Exception as e:
            self.logger().error(f"Error fetching trade updates for order {order.client_order_id}: {e}")

        return trade_updates

    async def _request_order_status(self, tracked_order: InFlightOrder) -> OrderUpdate:
        """
        Request order status from the exchange
        """
        try:
            symbol = await self.exchange_symbol_associated_to_pair(tracked_order.trading_pair)
            params = {
                "symbol": symbol,
                "orderId": tracked_order.exchange_order_id,
            }

            order_status = await self._api_get(
                path_url=CONSTANTS.ORDER_PATH_URL,
                params=params,
                is_auth_required=True,
            )

            new_state = CONSTANTS.ORDER_STATE_MAP.get(order_status.get("status"), OrderState.OPEN)

            order_update = OrderUpdate(
                trading_pair=tracked_order.trading_pair,
                update_timestamp=self.current_timestamp,
                new_state=new_state,
                client_order_id=tracked_order.client_order_id,
                exchange_order_id=tracked_order.exchange_order_id,
            )

            return order_update

        except Exception as e:
            self.logger().error(f"Error requesting order status: {e}")
            raise

    async def _update_balances(self):
        """
        Update account balances
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()

        balances_response = await self._api_get(
            path_url=CONSTANTS.BALANCES_PATH_URL,
            is_auth_required=True,
        )

        for asset_name, balance_data in balances_response.items():
            remote_asset_names.add(asset_name)
            available_balance = Decimal(balance_data.get("available", "0"))
            total_balance = available_balance + Decimal(balance_data.get("locked", "0"))

            self._account_available_balances[asset_name] = available_balance
            self._account_balances[asset_name] = total_balance

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    async def _update_positions(self):
        """
        Update account positions
        """
        positions_response = await self._api_get(
            path_url=CONSTANTS.POSITIONS_PATH_URL,
            is_auth_required=True,
        )

        for position_data in positions_response.get("positions", []):
            try:
                symbol = position_data.get("symbol", "")
                trading_pair = await self.trading_pair_associated_to_exchange_symbol(symbol)
                
                position_size = Decimal(position_data.get("size", "0"))
                if position_size == 0:
                    continue

                entry_price = Decimal(position_data.get("entryPrice", "0"))
                unrealized_pnl = Decimal(position_data.get("unrealizedPnl", "0"))
                
                position_side = PositionSide.LONG if position_size > 0 else PositionSide.SHORT
                abs_position_size = abs(position_size)

                position = Position(
                    trading_pair=trading_pair,
                    position_side=position_side,
                    unrealized_pnl=unrealized_pnl,
                    entry_price=entry_price,
                    amount=abs_position_size,
                )

                self._account_positions[trading_pair] = position

            except Exception as e:
                self.logger().error(f"Error processing position data: {e}", exc_info=True)

    async def _sleep(self, delay: float):
        """
        Sleep for the given delay
        """
        await asyncio.sleep(delay)