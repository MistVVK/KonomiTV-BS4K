import 'vuetify/styles';
import '@/styles/materialdesignicons.min.css';
import { createVuetify } from 'vuetify';
import { ja } from 'vuetify/locale';

import { KONOMITV_BS4K_THEMES } from '@/themes';


const vuetify = createVuetify({
    // 独自のメディアクエリ (styles/mixin.scss) と表示切り替えの境界を揃える
    display: {
        thresholds: {
            xs: 0,
            sm: 600,
            md: 960,
            lg: 1280,
            xl: 1920,
            xxl: 2560,
        },
    },
    locale: {
        locale: 'ja',
        fallback: 'ja',
        messages: { ja },
    },
    theme: {
        defaultTheme: 'KonomiClassic',
        themes: KONOMITV_BS4K_THEMES,
    },
});

export default vuetify;
