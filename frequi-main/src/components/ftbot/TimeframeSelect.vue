<script setup lang="ts">
interface Props {
  value?: string;
  belowTimeframe?: string;
  size?: undefined | 'sm' | 'md' | 'lg' | 'xl';
}

const props = withDefaults(defineProps<Props>(), {
  value: '',
  belowTimeframe: '',
  size: undefined,
});
const emit = defineEmits<{ input: [value: string] }>();

const selectedTimeframe = ref('');
// The below list must always remain sorted correctly!
const availableTimeframesBase = [
  // Placeholder value
  { value: null, label: '使用策略默认值' },
  { value: '1m', label: '1分钟' },
  { value: '3m', label: '3分钟' },
  { value: '5m', label: '5分钟' },
  { value: '15m', label: '15分钟' },
  { value: '30m', label: '30分钟' },
  { value: '1h', label: '1小时' },
  { value: '2h', label: '2小时' },
  { value: '4h', label: '4小时' },
  { value: '6h', label: '6小时' },
  { value: '8h', label: '8小时' },
  { value: '12h', label: '12小时' },
  { value: '1d', label: '1天' },
  { value: '3d', label: '3天' },
  { value: '1w', label: '1周' },
  { value: '2w', label: '2周' },
  { value: '1M', label: '1月' },
  { value: '1y', label: '1年' },
];

const availableTimeframes = computed(() => {
  if (!props.belowTimeframe) {
    return availableTimeframesBase;
  }
  const idx = availableTimeframesBase.findIndex((v) => v.value === props.belowTimeframe);

  return [...availableTimeframesBase].splice(0, idx);
});

const emitSelectedTimeframe = () => {
  emit('input', selectedTimeframe.value);
};
</script>

<template>
  <USelect
    v-model="selectedTimeframe"
    placeholder="使用策略默认值"
    :size="size"
    :items="availableTimeframes"
    @change="emitSelectedTimeframe"
  ></USelect>
</template>
