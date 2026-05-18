<script setup lang="ts">
import type { ForceExitPayload, Trade } from '@/types';
import { refDebounced } from '@vueuse/core';

export interface ForceExitFormProps {
  trade: Trade;
  stakeCurrencyDecimals: number;
}

const props = defineProps<ForceExitFormProps>();
const emit = defineEmits<{
  close: [value: boolean];
}>();

const botStore = useBotStore();

const form = ref<HTMLFormElement>();
const amount = ref<number | undefined>(undefined);
const price = ref<number | undefined>(undefined);
const ordertype = ref('limit');

const checkFormValidity = () => {
  const valid = form.value?.checkValidity();

  return valid;
};

async function handleExit() {
  // Exit when the form isn't valid
  if (!checkFormValidity()) {
    return;
  }
  // call forceentry
  const payload: ForceExitPayload = { tradeid: String(props.trade.trade_id) };

  if (ordertype.value) {
    payload.ordertype = ordertype.value;
  }
  if (amount.value) {
    payload.amount = amount.value;
  }
  if (price.value && botStore.activeBot.botFeatures.forceExitWithPrice) {
    payload.price = price.value;
  }
  await nextTick();
  botStore.activeBot.forceexit(payload);
  emit('close', true);
}

function resetForm() {
  amount.value = props.trade.amount;
  ordertype.value =
    botStore.activeBot.botState?.order_types?.force_exit ||
    botStore.activeBot.botState?.order_types?.exit ||
    'limit';
}

const amountDebounced = refDebounced(amount, 250, { maxWait: 500 });

const amountInBase = computed<string>(() => {
  return amountDebounced.value && props.trade.current_rate
    ? `~${formatPriceCurrency(amountDebounced.value * props.trade.current_rate, props.trade.quote_currency || '', props.stakeCurrencyDecimals)} (Estimated value) `
    : '';
});
const orderTypeOptions = [
  { value: 'market', text: '市价' },
  { value: 'limit', text: '限价' },
];
resetForm();
</script>

<template>
  <UModal :title="`强制退出交易`" description="配置并确认强制退出交易">
    <template #body>
      <form ref="form" class="space-y-4" @submit.prevent="handleExit">
        <div class="mb-4">
          <p class="mb-2">
            <span>正在退出交易 #{{ trade.trade_id }} {{ trade.pair }}.</span>
            <br />
            <span>当前持有 {{ trade.amount }} {{ trade.base_currency }}</span>
          </p>
        </div>

        <UFormField
          :label="`数量（${trade.base_currency}）[可选]`"
          :description="amountInBase"
        >
          <div class="space-y-2">
            <UInputNumber
              v-model="amount"
              :min="0"
              :max="trade.amount"
              :step="trade.amount_precision ?? 0.000001"
              :format-options="{
                maximumFractionDigits: 8,
              }"
              class="w-full"
            />
            <USlider
              v-model="amount"
              :min="0"
              :max="trade.amount"
              :step="trade.amount_precision ?? 0.000001"
              class="w-full"
            />
          </div>
        </UFormField>
        <UFormField
          label="价格"
          v-if="botStore.activeBot.botFeatures.forceExitWithPrice"
          description="仅适用于限价单"
        >
          <UInputNumber
            id="price-input"
            v-model="price"
            :disabled="ordertype !== 'limit'"
            :min="0"
            :step="trade.price_precision ?? 0.000001"
            :stepSnapping="false"
            :format-options="{
              maximumFractionDigits: 8,
            }"
            class="w-full"
          />
        </UFormField>

        <UFormField label="订单类型" required>
          <USegmentedControl
            v-model="ordertype"
            :items="orderTypeOptions"
            label-key="text"
            value-key="value"
            class="w-full"
          />
        </UFormField>
      </form>
    </template>
    <template #footer>
      <UButton class="ms-auto" icon="mdi:close" color="neutral" @click="$emit('close', false)"
        >取消</UButton
      >
      <UButton icon="mdi:exit-to-app" @click="handleExit">退出仓位</UButton>
    </template>
  </UModal>
</template>
