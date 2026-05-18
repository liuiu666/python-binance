<script setup lang="ts">
import { h } from 'vue';
import type { BalanceValues } from '@/types';

const botStore = useBotStore();
const hideSmallBalances = ref(true);
const showBotOnly = ref(true);

const smallBalance = computed<number>(() => {
  return Number((1.1 ** botStore.activeBot.stakeCurrencyDecimals).toFixed(8));
});

const canUseBotBalance = computed(() => {
  return botStore.activeBot.botFeatures.hasBotBalance;
});

const balanceCurrencies = computed(() => {
  return (
    botStore.activeBot.balance.currencies?.filter(
      (v) =>
        (!hideSmallBalances.value || v.est_stake >= smallBalance.value) &&
        (!canUseBotBalance.value || !showBotOnly.value || (v.is_bot_managed ?? true) === true),
    ) ?? []
  );
});

const tableData = computed<Record<string, string | number | null>[]>(() => {
  return balanceCurrencies.value.map((v) => ({
    currency: v.currency,
    free: v.free,
    bot_owned: v.bot_owned ?? null,
    est_stake: v.est_stake,
    est_stake_bot: v.est_stake_bot ?? null,
  }));
});

const formatCurrency = (value) => {
  return value ? formatPrice(value, botStore.activeBot.stakeCurrencyDecimals) : '';
};

const chartValues = computed<BalanceValues[]>(() => {
  return balanceCurrencies.value.map((v) => {
    return {
      balance:
        showBotOnly.value && canUseBotBalance.value && v.bot_owned != undefined
          ? v.bot_owned
          : v.is_position === true
            ? v.position
            : v.balance,
      currency: v.currency,
      est_stake:
        showBotOnly.value && canUseBotBalance.value
          ? (v.est_stake_bot ?? v.est_stake)
          : v.est_stake,
      free: showBotOnly.value && canUseBotBalance.value ? (v.bot_owned ?? v.free) : v.free,
      used: v.used,
      stake: v.stake,
    };
  });
});

const tableFields = computed(() => {
  return [
    { field: 'currency', header: '币种' },
    {
      field: showBotOnly.value && canUseBotBalance.value ? 'bot_owned' : 'free',
      header: '可用',
      asCurrency: true,
    },
    {
      field: showBotOnly.value && canUseBotBalance.value ? 'est_stake_bot' : 'est_stake',
      header: `以 ${botStore.activeBot.balance.stake} 计`,
      asCurrency: true,
    },
  ];
});

async function refreshBalance() {
  botStore.activeBot.getBalance();
}

const tableColumns = computed(() => {
  return tableFields.value.map((field, index) => ({
    accessorKey: field.field,
    header: field.header,
    cell: field.asCurrency
      ? ({ row }: { row: { original: Record<string, number | string | null> } }) =>
          formatCurrency(row.original[field.field])
      : undefined,
    footer:
      index === 0
        ? '总计'
        : index === 1
          ? () =>
              h(
                'span',
                {
                  class: 'italic',
                  title: `相比初始资金 ${formatCurrency(
                    botStore.activeBot.balance.starting_capital,
                  )} ${botStore.activeBot.balance.stake} 的增长`,
                },
                formatPercent(botStore.activeBot.balance.starting_capital_ratio),
              )
          : () =>
              h(
                'strong',
                {},
                showBotOnly.value && canUseBotBalance.value
                  ? formatCurrency(botStore.activeBot.balance.total_bot)
                  : formatCurrency(botStore.activeBot.balance.total),
              ),
  }));
});

onMounted(() => {
  refreshBalance();
});
</script>

<template>
  <div>
    <div class="flex flex-wrap flex-row mb-2 justify-end items-center">
      <label class="text-xl ms-1 me-auto mb-0">{{ showBotOnly ? '机器人' : '账户' }}余额</label>
      <div class="flex flex-row gap-1">
        <UButton
          v-if="canUseBotBalance"
          color="neutral"
          :tooltip="!showBotOnly ? '显示账户余额' : '显示机器人余额'"
          :icon="showBotOnly ? 'mdi:robot' : 'mdi:bank'"
          @click="showBotOnly = !showBotOnly"
        />
        <UButton
          color="neutral"
          :tooltip="!hideSmallBalances ? '隐藏小额余额' : '显示所有余额'"
          :icon="hideSmallBalances ? 'mdi:eye-off' : 'mdi:eye'"
          @click="hideSmallBalances = !hideSmallBalances"
        />
        <UButton color="neutral" icon="mdi:refresh" @click="refreshBalance" />
      </div>
    </div>
    <BalanceChart v-if="balanceCurrencies" :currencies="chartValues" />
    <div>
      <p v-if="botStore.activeBot.balance.note">
        <strong>{{ botStore.activeBot.balance.note }}</strong>
      </p>
      <UTable :data="tableData" :columns="tableColumns" />
    </div>
  </div>
</template>
