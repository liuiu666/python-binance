<script setup lang="ts">
import type { PeriodicBreakdown } from '@/types';
import type { TableColumn } from '@nuxt/ui';

const props = defineProps<{
  periodicBreakdown: PeriodicBreakdown;
}>();

const periodicBreakdownSelections = computed(() => {
  const res = [
    { value: 'day', label: '天' },
    { value: 'week', label: '周' },
    { value: 'month', label: '月' },
  ];
  if (props.periodicBreakdown.year) {
    res.push({ value: 'year', label: '年' });
  }
  if (props.periodicBreakdown.weekday) {
    res.push({ value: 'weekday', label: '工作日' });
  }

  return res;
});

const periodicBreakdownPeriod = ref<string>('month');

type PeriodRow = {
  date: string;
  trades?: number;
  profit_abs?: number;
  profit_factor?: number;
  wins?: number;
  draws?: number;
  losses?: number;
  loses?: number;
};

const columns: TableColumn<PeriodRow>[] = [
  { accessorKey: 'date', header: '日期' },
  { accessorKey: 'trades', header: '交易数' },
  { accessorKey: 'profit_abs', header: '总利润' },
  { accessorKey: 'profit_factor', header: '利润因子' },
  { accessorKey: 'wins', header: '盈利' },
  { accessorKey: 'draws', header: '平局' },
  { accessorKey: 'losses', header: '亏损' },
  { id: 'win_rate', header: '胜率' },
];
</script>

<template>
  <USegmentedControl
    v-model="periodicBreakdownPeriod"
    :items="periodicBreakdownSelections"
    value-key="value"
    size="md"
    class="m-2"
  ></USegmentedControl>
  <UTable :data="periodicBreakdown[periodicBreakdownPeriod]" :columns="columns">
    <template #trades-cell="{ row }">
      {{ row.original.trades ?? 'N/A' }}
    </template>
    <template #profit_abs-cell="{ row }">
      {{ formatNumber(row.original.profit_abs, 2) }}
    </template>
    <template #profit_factor-cell="{ row }">
      {{ formatPrice(row.original.profit_factor ?? null, 2) }}
    </template>
    <template #losses-cell="{ row }">
      {{ row.original.loses ?? row.original.losses ?? 'N/A' }}
    </template>
    <template #win_rate-cell="{ row }">
      {{
        formatPercent(
          row.original.wins! /
            (row.original.wins! +
              row.original.draws! +
              (row.original.loses ?? row.original.losses ?? 0)),
          2,
        )
      }}
    </template>
  </UTable>
</template>
