<script setup lang="ts">
import type { ForceEnterPayload } from '@/types';
import { OrderSides } from '@/types';

export interface ForceEntryFormProps {
  pair?: string;
  positionIncrease?: boolean;
}

const props = withDefaults(defineProps<ForceEntryFormProps>(), {
  pair: '',
  positionIncrease: false,
});

const emit = defineEmits<{
  close: [value: boolean];
}>();

const botStore = useBotStore();

const form = ref<HTMLFormElement>();
const selectedPair = ref('');
const price = ref<number | undefined>(undefined);
const stakeAmount = ref<number | undefined>(undefined);
const leverage = ref<number | undefined>(undefined);

const ordertype = ref('');
const orderSide = ref<OrderSides>(OrderSides.long);
const enterTag = ref('force_entry');

const orderTypeOptions = [
  { value: 'market', text: '市价' },
  { value: 'limit', text: '限价' },
];
const orderSideOptions = [
  { value: 'long', text: '做多' },
  { value: 'short', text: '做空' },
];

function checkFormValidity() {
  const valid = form.value?.checkValidity();

  return valid;
}

async function handleEntry() {
  // Exit when the form isn't valid
  if (!checkFormValidity()) {
    return;
  }

  // call forceentry
  const payload: ForceEnterPayload = { pair: selectedPair.value };
  if (price.value) {
    payload.price = Number(price.value);
  }
  if (ordertype.value) {
    payload.ordertype = ordertype.value;
  }
  if (stakeAmount.value) {
    payload.stakeamount = stakeAmount.value;
  }
  if (botStore.activeBot.botFeatures.forceEnterShort && botStore.activeBot.shortAllowed) {
    payload.side = orderSide.value;
  }
  if (botStore.activeBot.botFeatures.forceEntryTag && enterTag.value) {
    payload.entry_tag = enterTag.value;
  }

  if (leverage.value) {
    payload.leverage = leverage.value;
  }
  botStore.activeBot.forceentry(payload);
  emit('close', true);
}

function resetForm() {
  selectedPair.value = props.pair;
  price.value = undefined;
  stakeAmount.value = undefined;
  ordertype.value =
    botStore.activeBot.botState?.order_types?.forcebuy ||
    botStore.activeBot.botState?.order_types?.force_entry ||
    botStore.activeBot.botState?.order_types?.buy ||
    botStore.activeBot.botState?.order_types?.entry ||
    'limit';
}

resetForm();
</script>

<template>
  <UModal
    :title="positionIncrease ? `增加 ${pair} 的仓位` : '强制入场'"
    :description="positionIncrease ? '增加现有仓位' : '手动入场新交易'"
  >
    <template #body>
      <form ref="form" class="space-y-4" @submit.prevent="handleEntry">
        <UFormField
          v-if="botStore.activeBot.botFeatures.forceEnterShort && botStore.activeBot.shortAllowed"
          label="订单方向（做多或做空）"
        >
          <USegmentedControl
            v-model="orderSide"
            :items="orderSideOptions"
            label-key="text"
            value-key="value"
            size="sm"
            class="w-full"
          />
        </UFormField>

        <UFormField label="交易对" required>
          <UInput
            v-model="selectedPair"
            :disabled="positionIncrease"
            required
            class="w-full"
            @keydown.enter="handleEntry"
            @focus="($event.target as HTMLInputElement).select()"
          />
        </UFormField>

        <UFormField label="价格[可选]">
          <UInputNumber
            v-model="price"
            show-buttons
            :min="0"
            :stepSnapping="false"
            :format-options="{
              maximumFractionDigits: 8,
            }"
            :step="0.1"
            class="w-full"
            @keydown.enter="handleEntry"
          />
        </UFormField>

        <UFormField :label="`投入金额（${botStore.activeBot.stakeCurrency}）[可选]`">
          <UInputNumber
            v-model="stakeAmount"
            show-buttons
            :min="0"
            :stepSnapping="false"
            :step="['USDC', 'USDT'].includes(botStore.activeBot.stakeCurrency) ? 10 : 1"
            class="w-full"
            :format-options="{
              maximumFractionDigits: 5,
            }"
          />
        </UFormField>

        <UFormField
          v-if="botStore.activeBot.botFeatures.forceEnterShort && botStore.activeBot.shortAllowed"
          label="使用的杠杆[可选]"
        >
          <UInputNumber
            id="leverage-input"
            v-model="leverage"
            show-buttons
            :min="1"
            :step="1"
            :max-fraction-digits="1"
            class="w-full"
            @keydown.enter="handleEntry"
          />
        </UFormField>

        <UFormField label="订单类型">
          <USegmentedControl
            v-model="ordertype"
            :items="orderTypeOptions"
            label-key="text"
            value-key="value"
            size="sm"
            class="w-full"
          />
        </UFormField>

        <UFormField
          v-if="botStore.activeBot.botFeatures.forceEntryTag"
          label="* 自定义入场标签[可选]"
        >
          <UInput id="enterTag-input" v-model="enterTag" class="w-full" />
        </UFormField>
      </form>
    </template>
    <template #footer>
      <div class="ms-auto flex justify-end gap-2">
        <UButton color="neutral" @click="$emit('close', false)" icon="mdi:close"> 取消 </UButton>
        <UButton @click="handleEntry" icon="mdi:check"> 入场 </UButton>
      </div>
    </template>
  </UModal>
</template>
