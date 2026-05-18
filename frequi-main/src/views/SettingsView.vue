<script setup lang="ts">
import { FtWsMessageTypes } from '@/types/wsMessageTypes';

const settingsStore = useSettingsStore();
const colorStore = useColorStore();
const layoutStore = useLayoutStore();

const timezoneOptions = ['UTC', Intl.DateTimeFormat().resolvedOptions().timeZone];
const openTradesOptions = [
  { value: OpenTradeVizOptions.showPill, text: '在图标显示徽章' },
  { value: OpenTradeVizOptions.asTitle, text: '显示在标题栏' },
  { value: OpenTradeVizOptions.noOpenTrades, text: '不在顶栏显示持仓' },
];
const colorPreferenceOptions = [
  { value: ColorPreferences.GREEN_UP, text: '涨绿跌红（A股风格）' },
  { value: ColorPreferences.RED_UP, text: '涨红跌绿（欧美风格）' },
];

const resetDynamicLayout = () => {
  layoutStore.resetTradingLayout();
  layoutStore.resetDashboardLayout();
  showAlert('所有布局已重置。');
};
</script>

<template>
  <UCard class="mx-auto mt-3 p-4 max-w-4xl">
    <template #header><span class="text-2xl font-bold">FreqUI 系统设置</span></template>
    <div class="flex flex-col gap-4 text-start dark:text-neutral-300">
      <p class="text-left">UI 版本: {{ settingsStore.uiVersion }}</p>

      <div class="border border-neutral-400 rounded-sm p-4 space-y-4">
        <h4 class="text-xl font-semibold">界面设置</h4>

        <BaseCheckbox v-model="layoutStore.layoutLocked" class="space-y-1">
          锁定动态布局
          <template #hint>
            锁定动态布局，防止面板移动。也可以从顶部导航栏中设置。
          </template>
        </BaseCheckbox>

        <div class="flex flex-row items-center gap-2 space-y-2">
          <UButton color="neutral" size="md" class="mb-0" @click="resetDynamicLayout"
            >重置布局</UButton
          >
          <small class="text-sm block text-neutral-600 dark:text-neutral-400"
            >将所有动态布局恢复为默认状态。</small
          >
        </div>

        <USeparator />

        <div class="space-y-1">
          <label class="block text-sm">顶栏持仓数显示方式</label>
          <USelect
            v-model="settingsStore.openTradesInTitle"
            :items="openTradesOptions"
            label-key="text"
            value-key="value"
            class="w-full"
          />
          <small class="text-sm text-neutral-600 dark:text-neutral-400"
            >选择当前持仓数量的显示方式</small
          >
        </div>

        <div class="space-y-1">
          <label class="block text-sm">UTC 时区</label>
          <USelect v-model="settingsStore.timezone" :items="timezoneOptions" class="w-full" />
          <small class="text-sm text-neutral-600 dark:text-neutral-400"
            >选择时区（推荐使用 UTC，因为交易所通常以 UTC 运行）</small
          >
        </div>

        <BaseCheckbox v-model="settingsStore.backgroundSync" class="space-y-1">
          后台同步
          <template #hint> 在选择其他机器人时保持后台数据同步运行。 </template>
        </BaseCheckbox>

        <BaseCheckbox v-model="settingsStore.confirmDialog" class="space-y-1">
          强制平仓二次确认
          <template #hint
            >强制平仓时弹出确认对话框。<br />
            禁用后标题栏将显示 <i-mdi-run-fast class="text-yellow-300 inline" />
            <i-mdi-alert class="text-yellow-300 inline" />
            警告图标。
          </template>
        </BaseCheckbox>

        <BaseCheckbox v-model="settingsStore.multiPaneButtonsShowText" class="space-y-1">
          多面板按钮显示文字
          <template #hint
            >在多面板按钮上显示文字标签。禁用时仅显示图标。</template
          >
        </BaseCheckbox>
      </div>

      <div class="border border-neutral-400 rounded-sm p-4 space-y-4">
        <h4 class="text-lg font-semibold">图表设置</h4>

        <div class="space-y-1">
          <label class="block text-sm">坐标轴显示位置</label>
          <URadioGroup
            v-model="settingsStore.chartLabelSide"
            :items="[
              { label: '左侧', value: 'left' },
              { label: '右侧', value: 'right' },
            ]"
            orientation="horizontal"
          />
          <small class="text-sm text-neutral-600 dark:text-neutral-400">
            坐标轴刻度显示在图表的左侧还是右侧？
          </small>
        </div>

        <BaseCheckbox v-model="settingsStore.useHeikinAshiCandles" class="space-y-1">
          使用 Heikin Ashi K线
          <template #hint>在图表中使用 Heikin Ashi K线</template>
        </BaseCheckbox>

        <BaseCheckbox v-model="settingsStore.useReducedPairCalls" class="space-y-1">
          仅请求必要的数据列
          <template #hint
            >可减少大型数据集的传输量。当图表配置变更时可能需要额外请求。</template
          >
        </BaseCheckbox>

        <div>
          <p>默认显示的K线数量（默认：250）</p>
          <div class="flex flex-row gap-5 w-full items-center">
            <USlider
              v-model="settingsStore.chartDefaultCandleCount"
              class="flex-1"
              :step="50"
              :min="100"
              :max="2000"
            />
            <UInputNumber
              v-model="settingsStore.chartDefaultCandleCount"
              :step="50"
              :min="100"
              :max="2000"
              size="sm"
            />
          </div>
        </div>

        <div class="space-y-1">
          <label class="block">K线颜色偏好</label>
          <div class="flex flex-row gap-5 items-center">
            <URadioGroup
              v-model="colorStore.colorPreference"
              :items="colorPreferenceOptions"
              label-key="text"
              value-key="value"
              orientation="horizontal"
            >
              <template #label="{ item }">
                <div class="flex items-center">
                  <span class="mr-2">{{ item.text }}</span>
                  <UIcon
                    :name="
                      item.value === ColorPreferences.GREEN_UP
                        ? 'mdi:arrow-up-thin'
                        : 'mdi:arrow-down-thin'
                    "
                    :color="
                      item.value === ColorPreferences.GREEN_UP
                        ? colorStore.colorProfit
                        : colorStore.colorLoss
                    "
                    class="-ml-2 size-5"
                  />
                  <UIcon
                    :name="
                      item.value === ColorPreferences.GREEN_UP
                        ? 'mdi:arrow-down-thin'
                        : 'mdi:arrow-up-thin'
                    "
                    :color="
                      item.value === ColorPreferences.GREEN_UP
                        ? colorStore.colorLoss
                        : colorStore.colorProfit
                    "
                    class="-ml-2 size-5"
                  />
                </div>
              </template>
            </URadioGroup>
          </div>
        </div>
      </div>

      <div class="border rounded-sm border-neutral-400 p-4 space-y-4">
        <h4 class="text-lg font-semibold">通知设置</h4>
        <div class="space-y-2">
          <BaseCheckbox v-model="settingsStore.notifications[FtWsMessageTypes.entryFill]">
            开仓成功通知
          </BaseCheckbox>
          <BaseCheckbox v-model="settingsStore.notifications[FtWsMessageTypes.exitFill]">
            平仓成功通知
          </BaseCheckbox>
          <BaseCheckbox v-model="settingsStore.notifications[FtWsMessageTypes.entryCancel]">
            开仓撤单通知
          </BaseCheckbox>
          <BaseCheckbox v-model="settingsStore.notifications[FtWsMessageTypes.exitCancel]">
            平仓撤单通知
          </BaseCheckbox>
        </div>
      </div>

      <div class="border rounded-sm border-neutral-400 p-4 space-y-4">
        <h4 class="text-lg font-semibold">回测设置</h4>
        <div>
          <label for="backtestMetrics" class="block">回测指标</label>
          <USelectMenu
            multiple
            id="backtestMetrics"
            v-model="settingsStore.backtestAdditionalMetrics"
            :items="availableBacktestMetrics"
            label-key="header"
            value-key="field"
            class="w-full"
            display="chip"
          />
          <small class="text-sm text-neutral-600 dark:text-neutral-400"
            >选择需要在每个交易对/标签层面展示的指标。</small
          >
        </div>
      </div>
    </div>
  </UCard>
</template>
