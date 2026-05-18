<script setup lang="ts">
import type { IndicatorConfig, PlotConfig } from '@/types';

const props = withDefaults(
  defineProps<{
    columns: string[];
    isVisible?: boolean;
  }>(),
  {
    isVisible: false,
  },
);

const plotStore = usePlotConfigStore();
const botStore = useBotStore();

const plotConfigNameLoc = ref('default');
const selIndicatorName = ref('');
const addNewIndicator = ref(false);
const showConfig = ref(false);
const selSubPlot = ref('main_plot');
const tempPlotConfig = ref<PlotConfig>();
const tempPlotConfigValid = ref(true);

const isMainPlot = computed(() => {
  return selSubPlot.value === 'main_plot';
});

const currentPlotConfig = computed(() => {
  if (isMainPlot.value) {
    return plotStore.editablePlotConfig.main_plot;
  }

  return plotStore.editablePlotConfig.subplots[selSubPlot.value];
});
const subplots = computed((): string[] => {
  // Subplot keys (for selection window)
  return ['main_plot', ...Object.keys(plotStore.editablePlotConfig.subplots)];
});
const usedColumns = computed((): { label: string; value: string }[] => {
  let usedCols: string[] = [];
  if (isMainPlot.value) {
    usedCols = Object.keys(plotStore.editablePlotConfig.main_plot);
  }
  const selSubPlot_ = plotStore.editablePlotConfig.subplots[selSubPlot.value];
  if (selSubPlot_) {
    usedCols = Object.keys(selSubPlot_);
  }
  return usedCols.map((col) => ({
    value: col,
    label: !props.columns.includes(col) ? `${col} <-- 此图表中不可用` : col,
  }));
});

function addIndicator(newIndicator: Record<string, IndicatorConfig>) {
  console.log('Adding indicator', newIndicator);
  // const { plotConfig.value } = this;
  const name = Object.keys(newIndicator)[0];
  if (!name) return;

  const indicator = newIndicator[name];
  if (isMainPlot.value) {
    // console.log(`Adding ${name} to MainPlot`);
    plotStore.editablePlotConfig.main_plot[name] = { ...indicator };
  } else {
    // console.log(`Adding ${name} to ${selSubPlot.value}`);
    plotStore.editablePlotConfig.subplots[selSubPlot.value]![name] = { ...indicator };
  }

  plotStore.editablePlotConfig = { ...plotStore.editablePlotConfig };
  // Reset random color
  addNewIndicator.value = false;
}

const selIndicator = computed<Record<string, IndicatorConfig>>({
  get() {
    if (addNewIndicator.value) {
      return {};
    }
    if (selIndicatorName.value) {
      const currentIndicator = currentPlotConfig.value?.[selIndicatorName.value];
      if (currentIndicator) {
        return {
          [selIndicatorName.value]: currentIndicator,
        };
      }
    }
    return {};
  },
  set(newValue: Record<string, IndicatorConfig>) {
    const name = Object.keys(newValue)[0];
    // this.currentPlotConfig[this.selIndicatorName] = { ...newValue[name] };
    // this.emitPlotConfig();
    if (name && newValue) {
      addIndicator(newValue);
    } else {
      addNewIndicator.value = false;
    }
  },
});

const plotConfigJson = computed({
  get() {
    return JSON.stringify(plotStore.editablePlotConfig, null, 2);
  },
  set(newValue: string) {
    try {
      tempPlotConfig.value = JSON.parse(newValue);
      // TODO: Should Validate schema validity (should be PlotConfig type...)
      tempPlotConfigValid.value = true;
    } catch (err) {
      tempPlotConfigValid.value = false;
    }
  },
});

function removeIndicator() {
  if (isMainPlot.value) {
    console.log(`Removing ${selIndicatorName.value} from MainPlot`);
    delete plotStore.editablePlotConfig.main_plot[selIndicatorName.value];
  } else {
    console.log(`Removing ${selIndicatorName.value} from ${selSubPlot.value}`);
    delete plotStore.editablePlotConfig.subplots[selSubPlot.value]?.[selIndicatorName.value];
  }

  plotStore.editablePlotConfig = { ...plotStore.editablePlotConfig };
  selIndicatorName.value = '';
}

function clickAddNewIndicator() {
  addNewIndicator.value = !addNewIndicator.value;
  selIndicatorName.value = '';
}

function addSubplot(newSubplotName: string) {
  plotStore.editablePlotConfig.subplots = {
    ...plotStore.editablePlotConfig.subplots,
    [newSubplotName]: {},
  };
  selSubPlot.value = newSubplotName;
}

function deleteSubplot(subplotName: string) {
  delete plotStore.editablePlotConfig.subplots[subplotName];
  // Reassign to trigger reactivity
  plotStore.editablePlotConfig = { ...plotStore.editablePlotConfig };
  selSubPlot.value = subplots.value[subplots.value.length - 1] ?? 'main_plot';
}

function renameSubplot(oldName: string, newName: string) {
  const oldSubPlot = plotStore.editablePlotConfig.subplots[oldName];
  if (oldSubPlot) {
    plotStore.editablePlotConfig.subplots[newName] = oldSubPlot;
  }
  selSubPlot.value = newName;
  delete plotStore.editablePlotConfig.subplots[oldName];
}

function loadPlotConfig() {
  // Reset from store
  const existingConf = plotStore.customPlotConfigs[plotStore.plotConfigName];
  if (existingConf) {
    plotStore.editablePlotConfig = deepClone(existingConf);
  }
}

function loadConfigFromString() {
  if (tempPlotConfig.value !== undefined && tempPlotConfigValid.value) {
    plotStore.editablePlotConfig = tempPlotConfig.value;
  }
}

// function clearConfig() {
//   // Use empty config
//   plotStore.editablePlotConfig = { ...EMPTY_PLOTCONFIG };
// }

async function loadPlotConfigFromStrategy() {
  if (botStore.activeBot.isWebserverMode && !botStore.activeBot.strategy?.strategy) {
    showAlert(`未选择策略，无法加载图表配置。`);
    return;
  }
  try {
    const strategyPlotConfig = await botStore.activeBot.getStrategyPlotConfig();
    if (strategyPlotConfig) {
      plotStore.editablePlotConfig = strategyPlotConfig;
    }
  } catch (error) {
    //
    showAlert('从策略加载图表配置失败。');
  }
}

function savePlotConfig() {
  plotStore.saveCustomPlotConfig(plotConfigNameLoc.value, plotStore.editablePlotConfig);
}

function addNewIndicatorSelected(indicator?: string) {
  addNewIndicator.value = false;

  if (indicator) {
    addIndicator({
      [indicator]: {
        color: randomColor(),
      },
    });
    selIndicatorName.value = indicator;
  }
}

watch(selSubPlot, () => {
  // Deselect Indicator when switching selected plot
  selIndicatorName.value = '';
});

watch(
  () => plotStore.plotConfigName,
  () => {
    selIndicatorName.value = '';
    // selSubPlot.value = '';
    plotConfigNameLoc.value = plotStore.plotConfigName;
  },
);

watch(
  () => props.isVisible,
  () => {
    if (props.isVisible) {
      // Deep clone and assign to editable
      plotStore.editablePlotConfig = deepClone(plotStore.plotConfig);
      plotStore.isEditing = true;
      plotConfigNameLoc.value = plotStore.plotConfigName;
    } else {
      plotStore.isEditing = false;
    }
  },
  {
    immediate: true,
  },
);
const fromPlotTemplateVisible = ref(false);

const showTagsInTooltips = computed({
  get() {
    return plotStore.editablePlotConfig.options?.showTags ?? true;
  },
  set(value: boolean) {
    if (!plotStore.editablePlotConfig.options) {
      plotStore.editablePlotConfig.options = {};
    }
    plotStore.editablePlotConfig.options.showTags = value;
  },
});
const markAreaZIndex = computed({
  get() {
    return plotStore.editablePlotConfig.options?.markAreaZIndex ?? 1;
  },
  set(value: number) {
    if (!plotStore.editablePlotConfig.options) {
      plotStore.editablePlotConfig.options = {};
    }
    plotStore.editablePlotConfig.options.markAreaZIndex = value;
  },
});
</script>

<template>
  <div v-if="columns">
    <UFormField label="图表配置名称" class="text-md">
      <PlotConfigSelect allow-edit></PlotConfigSelect>
    </UFormField>
    <USeparator class="my-2" />
    <BaseCheckbox v-model="showTagsInTooltips" class="mb-1">在提示中显示标签</BaseCheckbox>
    <div class="grid grid-cols-2 items-center gap-2 w-full">
      <label>标记区域 Z-Index <br /><small>(默认为 1 - K线图在 Z=2)</small></label>

      <UInputNumber v-model="markAreaZIndex" class="mb-1" />
    </div>
    <USeparator class="my-2" />

    <UFormField label="目标图表" class="text-md">
      <EditValue
        v-model="selSubPlot"
        :allow-edit="!isMainPlot"
        allow-add
        editable-name="plot configuration"
        align-vertical
        @new="addSubplot"
        @delete="deleteSubplot"
        @rename="renameSubplot"
      >
        <UListbox
          id="fieldSel"
          v-model="selSubPlot"
          value-key="value"
          :items="
            subplots.map((plot) => ({
              value: plot,
              label: plot,
            }))
          "
        >
        </UListbox>
      </EditValue>
    </UFormField>
    <USeparator class="my-2" />
    <UFormField label="此图表中的指标" class="text-md">
      <UListbox v-model="selIndicatorName" value-key="value" :items="usedColumns"> </UListbox>
    </UFormField>
    <div class="flex flex-row mt-1 gap-1">
      <UButton
        color="neutral"
        title="从图表中移除指标"
        :disabled="!selIndicatorName"
        class="col"
        @click="removeIndicator"
        label="移除指标"
        icon="mdi:minus-box-outline"
      />

      <UButton
        color="neutral"
        title="从模板加载指标配置"
        @click="fromPlotTemplateVisible = !fromPlotTemplateVisible"
        label="从模板"
        icon="mdi:folder-arrow-down-outline"
      />

      <UButton
        title="添加指标到图表"
        icon="mdi:plus-box-outline"
        class="col"
        :disabled="addNewIndicator"
        @click="clickAddNewIndicator"
        label="添加指标"
      />
    </div>

    <PlotIndicatorSelect
      v-if="addNewIndicator"
      :columns="columns"
      class="mt-1"
      label="选择要添加的指标"
      @indicator-selected="addNewIndicatorSelected"
    />

    <PlotFromTemplate v-model:visible="fromPlotTemplateVisible" :columns="columns" />

    <PlotIndicator
      v-if="selIndicatorName && !fromPlotTemplateVisible"
      v-model="selIndicator"
      class="mt-1"
      :columns="columns"
    />
    <USeparator class="my-2" />

    <div class="flex flex-row gap-1">
      <UButton
        color="neutral"
        :disabled="addNewIndicator"
        title="重置为上次保存的配置"
        @click="loadPlotConfig"
        label="重置"
        icon="mdi:restore"
      />

      <!--
        Does Resetting a config to "nothing" make sense, or can this be done via "delete / create"?
        <UButton
        class="ms-1 "
        color="neutral"
        :disabled="addNewIndicator"
        title="Start with empty configuration"
        @click="clearConfig"
        >Reset</UButton
      > -->
      <UButton
        :disabled="
          (botStore.activeBot.isWebserverMode &&
            !botStore.activeBot.botFeatures.plotConfigFromServer) ||
          !botStore.activeBot.isBotOnline ||
          addNewIndicator
        "
        color="neutral"
        label="从策略"
        icon="mdi:download"
        @click="loadPlotConfigFromStrategy"
      />

      <UButton
        id="showButton"
        color="neutral"
        :disabled="addNewIndicator"
        title="显示配置以便轻松复制到策略"
        @click="showConfig = !showConfig"
        :icon="showConfig ? 'mdi:eye-off' : 'mdi:eye'"
        :label="showConfig ? '隐藏' : '显示'"
      />

      <UButton
        data-toggle="tooltip"
        :disabled="addNewIndicator"
        title="保存配置"
        @click="savePlotConfig"
        label="保存"
        variant="solid"
        icon="mdi:content-save"
      />
    </div>
    <UButton
      v-if="showConfig"
      class="mt-1"
      color="neutral"
      size="sm"
      title="从下方文本框加载配置"
        @click="loadConfigFromString"
        icon="mdi:upload"
        >从下方字符串加载</UButton
    >
    <div v-if="showConfig" class="w-full ms-1 mt-2">
      <UTextarea
        id="TextArea"
        v-model="plotConfigJson"
        class="w-full"
        autoresize
        :maxrows="10"
        :state="tempPlotConfigValid"
      >
      </UTextarea>
    </div>
  </div>
</template>
