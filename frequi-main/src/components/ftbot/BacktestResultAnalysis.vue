<script setup lang="ts">
import type { StrategyBacktestResult } from '@/types';

const props = defineProps<{
  backtestResult: StrategyBacktestResult;
}>();

const backtestResultStats = computed(() => {
  const tmp = generateBacktestMetricRows(props.backtestResult);
  return formatObjectForTable({ value: tmp }, 'metric');
});

const backtestResultSettings = computed(() => {
  // Transpose Result into readable format
  const tmp = generateBacktestSettingRows(props.backtestResult);

  return formatObjectForTable({ value: tmp }, 'setting');
});
</script>

<template>
  <div class="px-0 w-full">
    <div class="flex justify-center">
      <h3 class="font-bold text-2xl mb-2">
        {{ backtestResult.strategy_name }}的回测结果
      </h3>
    </div>

    <div class="flex flex-col text-start ms-0 me-2 gap-2">
      <div class="flex flex-col xl:flex-row">
        <div class="px-0 px-xl-0 pe-xl-1 grow">
          <DraggableContainer header="策略设置">
            <UTable
              :data="backtestResultSettings"
              :columns="[
                { accessorKey: 'setting', header: '设置' },
                { accessorKey: 'value', header: '值' },
              ]"
            />
          </DraggableContainer>
        </div>
        <div class="px-0 xl:px-0 pt-2 xl:pt-0 xl:ps-1 grow">
          <DraggableContainer header="指标">
            <UTable
              :data="backtestResultStats"
              :columns="[
                { accessorKey: 'metric', header: '指标' },
                { accessorKey: 'value', header: '值' },
              ]"
            />
          </DraggableContainer>
        </div>
      </div>
      <BacktestResultTablePer
        title="按入场标签统计结果"
        :results="backtestResult.results_per_enter_tag"
        :stake-currency="backtestResult.stake_currency"
        key-header="入场标签"
        :stake-currency-decimals="backtestResult.stake_currency_decimals"
      />

      <BacktestResultTablePer
        title="按退出原因统计结果"
        :results="backtestResult.exit_reason_summary ?? []"
        :stake-currency="backtestResult.stake_currency"
        key-header="退出原因"
        :stake-currency-decimals="backtestResult.stake_currency_decimals"
      />

      <BacktestResultTablePer
        v-if="backtestResult.mix_tag_stats"
        title="混合标签统计结果"
        :results="backtestResult.mix_tag_stats ?? []"
        :stake-currency="backtestResult.stake_currency"
        :key-headers="['入场标签', '退出标签']"
        :stake-currency-decimals="backtestResult.stake_currency_decimals"
      />

      <BacktestResultTablePer
        title="按交易对统计结果"
        :results="backtestResult.results_per_pair"
        :stake-currency="backtestResult.stake_currency"
        key-header="交易对"
        :stake-currency-decimals="backtestResult.stake_currency_decimals"
      />
      <DraggableContainer v-if="backtestResult.periodic_breakdown" header="周期细分">
        <BacktestResultPeriodBreakdown :periodic-breakdown="backtestResult.periodic_breakdown">
        </BacktestResultPeriodBreakdown>
      </DraggableContainer>

      <DraggableContainer header="单个交易">
        <TradeList
          :trades="backtestResult.trades"
          :show-filter="true"
          :stake-currency="backtestResult.stake_currency"
        />
      </DraggableContainer>
    </div>
  </div>
</template>
