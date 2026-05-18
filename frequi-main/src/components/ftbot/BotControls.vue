<script setup lang="ts">
import type { ForceExitPayload } from '@/types';

const botStore = useBotStore();
const { confirm } = useConfirmBox();

const { forceEntryDialog } = useForceTrade();

const isRunning = computed((): boolean => {
  return botStore.activeBot.botState?.state === 'running';
});

async function handleStopBot() {
  const result = await confirm({
    title: '停止机器人',
    message: '确定要停止机器人运行吗？',
  });
  if (result) {
    botStore.activeBot.stopBot();
  }
}

async function handleStopBuy() {
  if (
    await confirm({
      title: '暂停 - 停止入场',
      message:
        'Freqtrade 将继续处理持仓交易，但不会进入新交易或增加仓位。\n确定要停止入场吗？',
    })
  ) {
    botStore.activeBot.stopBuy();
  }
}

async function handleReloadConfig() {
  if (
    await confirm({
      title: '重新加载配置',
      message: '重新加载配置（包括策略）？',
    })
  ) {
    botStore.activeBot.reloadConfig();
  }
}

async function handleForceExit() {
  if (
    await confirm({
      title: '强制退出全部',
      message: '确定要强制退出所有交易吗？',
    })
  ) {
    const payload: ForceExitPayload = {
      tradeid: 'all',
      // TODO: support ordertype (?)
    };
    botStore.activeBot.forceexit(payload);
  }
}

async function handleForceEntry() {
  await forceEntryDialog({
    pair: botStore.activeBot.selectedPair,
  });
}
</script>

<template>
  <div class="flex flex-row gap-1">
    <UButton
      size="xl"
      color="neutral"
      :disabled="!botStore.activeBot.isTrading || isRunning"
      title="开始交易"
      icon="mdi:play"
      @click="botStore.activeBot.startBot()"
    />
    <UButton
      size="xl"
      color="neutral"
      :disabled="!botStore.activeBot.isTrading || !isRunning"
      title="停止交易 - 同时停止处理所有持仓。"
      icon="mdi:stop"
      @click="handleStopBot()"
    />
    <UButton
      size="xl"
      color="neutral"
      :disabled="!botStore.activeBot.isTrading || !isRunning"
      title="暂停 (StopBuy) - Freqtrade 将继续处理持仓交易，但不会进入新交易或增加仓位。"
      icon="mdi:pause"
      @click="handleStopBuy()"
    />
    <UButton
      size="xl"
      color="neutral"
      :disabled="!botStore.activeBot.isTrading"
      title="重新加载配置 - 重新加载包括策略在内的配置，重置所有临时更改的设置。"
      icon="mdi:reload"
      @click="handleReloadConfig()"
    />
    <UButton
      color="neutral"
      size="xl"
      :disabled="!botStore.activeBot.isTrading"
      title="强制退出全部"
      icon="mdi:close-box-multiple"
      @click="handleForceExit()"
    />
    <UButton
      v-if="botStore.activeBot.botState && botStore.activeBot.botState.force_entry_enable"
      size="xl"
      color="neutral"
      :disabled="!botStore.activeBot.isTrading || !isRunning"
      title="强制入场 - 立即以可选价格进入交易。退出将根据策略规则处理。"
      icon="mdi:plus-box-multiple-outline"
      @click="handleForceEntry"
    />
    <UButton
      v-if="botStore.activeBot.isWebserverMode && false"
      size="xl"
      color="neutral"
      :disabled="botStore.activeBot.isTrading"
      title="启动交易模式"
      icon="mdi:play"
      @click="botStore.activeBot.startTrade()"
    />
  </div>
</template>
