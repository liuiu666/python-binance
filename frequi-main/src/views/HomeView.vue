<template>
  <div class="mt-5">
    <div class="flex justify-center">
      <BotList class="max-w-xl" />
    </div>
    <USeparator class="my-5" />
    <AppIcon class="my-5 sm:w-80 sm:h-80 w-64 h-64" />

    <h1 class="font-bold text-2xl sm:text-4xl mb-4 transition-all">
      欢迎使用 <AppText class="text-2xl! sm:text-4xl!" />
    </h1>
    <div>此页面允许您控制您的交易机器人。</div>
    <USeparator class="my-5" />
    <p class="mb-2">
      如果您需要帮助，请参考
      <a
        class="text-primary underline cursor-pointer"
        href="https://www.freqtrade.io/en/latest/"
        target="_blank"
      >
        Freqtrade 文档
      </a>
      。
    </p>

    <p class="mb-5">祝您愉快 - <i>Freqtrade 团队敬上</i></p>
  </div>
</template>
