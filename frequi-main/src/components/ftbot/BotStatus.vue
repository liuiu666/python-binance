<script setup lang="ts">
const botStore = useBotStore();
</script>

<template>
  <div v-if="botStore.activeBot.botState" class="p-4">
    <p class="mb-4">
      正在运行 Freqtrade <strong>{{ botStore.activeBot.version }}</strong>
    </p>
    <p class="mb-4">
      运行配置：
      <strong>
        {{ botStore.activeBot.botState.max_open_trades }}x{{
          botStore.activeBot.botState.stake_amount
        }}
        {{ botStore.activeBot.botState.stake_currency }}
      </strong>
      ，交易所：
      <strong class="text-nowrap"
        >{{ botStore.activeBot.botState.exchange }}
        {{ botStore.activeBot.botState.demo_trading ? '(演示)' : '' }}</strong
      >
      ，交易模式：
      <strong
        >{{ botStore.activeBot.botState.trading_mode || 'spot' }}
        {{
          botStore.activeBot.botState.trading_mode !== 'spot'
            ? (botStore.activeBot.botState.margin_mode ?? '')
            : ''
        }}</strong
      >
      ，策略：<strong>{{ botStore.activeBot.botState.strategy }}</strong
      >。
    </p>
    <p v-if="'stoploss_on_exchange' in botStore.activeBot.botState" class="mb-4">
      交易所止损
      <strong>{{
        botStore.activeBot.botState.stoploss_on_exchange ? '已启用' : '已禁用'
      }}</strong
      >。
    </p>
    <p class="mb-4">
      当前状态：<strong>{{ botStore.activeBot.botState.state }}</strong
      >，
      <strong>强制入场: {{ botStore.activeBot.botState.force_entry_enable }}</strong>
    </p>
    <p>
      <strong>{{ botStore.activeBot.botState.dry_run ? '模拟运行' : '实盘运行' }}</strong>
    </p>
    <USeparator class="my-2" />
    <p class="mb-4" v-if="botStore.activeBot.profit">
      平均盈亏 {{ formatPercent(botStore.activeBot.profit.profit_all_ratio_mean) }} (总计
      {{ formatPercent(botStore.activeBot.profit.profit_all_ratio_sum) }})，共
      {{ botStore.activeBot.profit.trade_count }} 笔交易，平均持仓时长
      {{ botStore.activeBot.profit.avg_duration }}。最佳交易对：
      {{ botStore.activeBot.profit.best_pair }}。
    </p>
    <p v-if="botStore.activeBot.profit?.first_trade_timestamp" class="mb-4">
      <span v-if="botStore.activeBot.profit.bot_start_timestamp" class="block">
        机器人启动日期：
        <strong>
          <DateTimeTZ :date="botStore.activeBot.profit.bot_start_timestamp" show-timezone />
        </strong>
      </span>
      <span class="block">
        首笔交易开仓：
        <strong>
          <DateTimeTZ :date="botStore.activeBot.profit.first_trade_timestamp" show-timezone />
        </strong>
      </span>
      <span class="block">
        最近交易开仓：
        <strong>
          <DateTimeTZ :date="botStore.activeBot.profit.latest_trade_timestamp" show-timezone />
        </strong>
      </span>
    </p>
    <p>
      <span v-if="botStore.activeBot.profit?.profit_factor" class="block">
        盈利因子：
        {{ formatNumber(botStore.activeBot.profit?.profit_factor, 2) }}
      </span>
      <span v-if="botStore.activeBot.profit?.trading_volume" class="block mb-4">
        交易量：
        {{
          formatPriceCurrency(
            botStore.activeBot.profit.trading_volume,
            botStore.activeBot.botState.stake_currency,
            botStore.activeBot.botState.stake_currency_decimals ?? 3,
          )
        }}
      </span>
    </p>
    <BaseCollapsible v-if="botStore.activeBot.strategy?.params" title="策略参数">
      <StrategyParameters :strategy="botStore.activeBot.strategy" class="m-3" />
    </BaseCollapsible>
    <USeparator class="my-5" />
    <BotProfit
      class="mx-1"
      v-if="botStore.activeBot.profitAll"
      :profit-all="botStore.activeBot.profitAll"
      :stake-currency="botStore.activeBot.botState.stake_currency ?? 'USDT'"
      :stake-currency-decimals="botStore.activeBot.botState.stake_currency_decimals ?? 3"
    />
  </div>
</template>
