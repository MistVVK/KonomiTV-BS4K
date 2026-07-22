import type { ThemeDefinition } from 'vuetify';


export type KonomiTVBS4KTheme =
    'KonomiClassic' |
    'KonomiNavy' |
    'KonomiCharcoal' |
    'DeepPlum' |
    'NightBlue' |
    'DayBlue' |
    'KonomiIvory' |
    'PearlBlue' |
    'WarmCream' |
    'CoolGray';

export interface IKonomiTVBS4KThemeOption {
    title: string;
    value: KonomiTVBS4KTheme;
    dark: boolean;
    preview: {
        background: string;
        surface: string;
        elevated: string;
        primary: string;
        accent: string;
        text: string;
        textMuted: string;
    };
}

interface IKonomiTVBS4KThemePalette {
    dark: boolean;
    background: string;
    surface: string;
    vuetifySurface?: string;
    surfaceBright?: string;
    elevated: string;
    elevatedHigh: string;
    primary: string;
    secondary: string;
    accent: string;
    text: string;
    textMuted: string;
    textSubtle: string;
    textDisabled: string;
    gray: string;
    black: string;
    navigationActive: string;
    playerOverlay: string;
    playerOnOverlay: string;
    legacyColorVariants?: boolean;
}

const mixHexColors = (base: string, target: string, targetRatio: number): string => {
    const baseValue = Number.parseInt(base.slice(1), 16);
    const targetValue = Number.parseInt(target.slice(1), 16);
    const mixed = [16, 8, 0].map((shift) => {
        const baseChannel = (baseValue >> shift) & 0xff;
        const targetChannel = (targetValue >> shift) & 0xff;
        return Math.round(baseChannel + (targetChannel - baseChannel) * targetRatio);
    });
    return `#${mixed.map(channel => channel.toString(16).padStart(2, '0')).join('')}`;
};

const createColorVariants = (color: string): Record<string, string> => ({
    'lighten-5': mixHexColors(color, '#ffffff', 0.85),
    'lighten-4': mixHexColors(color, '#ffffff', 0.68),
    'lighten-3': mixHexColors(color, '#ffffff', 0.51),
    'lighten-2': mixHexColors(color, '#ffffff', 0.34),
    'lighten-1': mixHexColors(color, '#ffffff', 0.17),
    'darken-1': mixHexColors(color, '#000000', 0.17),
    'darken-2': mixHexColors(color, '#000000', 0.34),
    'darken-3': mixHexColors(color, '#000000', 0.51),
    'darken-4': mixHexColors(color, '#000000', 0.68),
});

const functionalColors = {
    'success': '#4caf50',
    'success-lighten-5': '#dcffd6', 'success-lighten-4': '#beffba', 'success-lighten-3': '#a2ff9e',
    'success-lighten-2': '#85e783', 'success-lighten-1': '#69cb69', 'success-darken-1': '#2d9437',
    'success-darken-2': '#00791e', 'success-darken-3': '#006000', 'success-darken-4': '#004700',
    'warning': '#fb8c00',
    'warning-lighten-5': '#ffff9e', 'warning-lighten-4': '#fffb82', 'warning-lighten-3': '#ffdf67',
    'warning-lighten-2': '#ffc24b', 'warning-lighten-1': '#ffa72d', 'warning-darken-1': '#db7200',
    'warning-darken-2': '#bb5900', 'warning-darken-3': '#9d4000', 'warning-darken-4': '#802700',
    'error': '#ff5252',
    'error-lighten-5': '#ffe4d5', 'error-lighten-4': '#ffc6b9', 'error-lighten-3': '#ffa99e',
    'error-lighten-2': '#ff8c84', 'error-lighten-1': '#ff6f6a', 'error-darken-1': '#df323b',
    'error-darken-2': '#bf0025', 'error-darken-3': '#9f0010', 'error-darken-4': '#800000',
    'info': '#2196f3',
    'info-lighten-5': '#d4ffff', 'info-lighten-4': '#b5ffff', 'info-lighten-3': '#95e8ff',
    'info-lighten-2': '#75ccff', 'info-lighten-1': '#51b0ff', 'info-darken-1': '#007cd6',
    'info-darken-2': '#0064ba', 'info-darken-3': '#004d9f', 'info-darken-4': '#003784',
    'twitter': '#4f82e6',
    'twitter-lighten-1': '#799fec',
    'twitter-lighten-2': '#41a5f1',
    'record-normal': '#31c3e3',
    'record-auto': '#2385e1',
    'comment-own': '#ff7088',
    'comment-own-outline': '#e33157',
    'jikkyo-festival': '#e7556e',
    'jikkyo-so-many': '#e76b55',
    'jikkyo-many': '#e7a355',
};

const createTheme = (palette: IKonomiTVBS4KThemePalette): ThemeDefinition => {
    const primaryVariants = createColorVariants(palette.primary);
    const secondaryVariants = createColorVariants(palette.secondary);
    const accentVariants = createColorVariants(palette.accent);
    // アクセント面上の文字色は、テーマの明暗だけで決めると中間色でコントラストが不足する。
    // 各アクセント色に対して、テーマ固有の黒と白のうちコントラストが高い方を選ぶ。
    const getRelativeLuminance = (color: string): number => {
        const colorValue = Number.parseInt(color.slice(1), 16);
        const channels = [16, 8, 0].map((shift) => ((colorValue >> shift) & 0xff) / 255);
        const linearChannels = channels.map(channel => channel <= 0.04045 ?
            channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
        return 0.2126 * linearChannels[0] + 0.7152 * linearChannels[1] + 0.0722 * linearChannels[2];
    };
    const getContrastRatio = (color1: string, color2: string): number => {
        const luminances = [getRelativeLuminance(color1), getRelativeLuminance(color2)].sort((a, b) => b - a);
        return (luminances[0] + 0.05) / (luminances[1] + 0.05);
    };
    const getReadableOnColor = (background: string): string => {
        return getContrastRatio(background, palette.black) >= getContrastRatio(background, '#ffffff') ?
            palette.black : '#ffffff';
    };
    const onPrimary = getReadableOnColor(palette.primary);
    const onSecondary = getReadableOnColor(palette.secondary);
    const onAccent = getReadableOnColor(palette.accent);

    // Konomi Classic だけは、従来の Vuetify 2 由来の段階色を維持する
    if (palette.legacyColorVariants === true) {
        Object.assign(primaryVariants, {
            'lighten-5': '#ffe0ff', 'lighten-4': '#ffc2ff', 'lighten-3': '#ffa5e9', 'lighten-2': '#ff89cd',
            'lighten-1': '#ff6cb2', 'darken-1': '#c8307d', 'darken-2': '#aa0064', 'darken-3': '#8d004c', 'darken-4': '#700036',
        });
        Object.assign(secondaryVariants, {
            'lighten-5': '#ffc7d9', 'lighten-4': '#ffaabe', 'lighten-3': '#ff8da3', 'lighten-2': '#ff7088',
            'lighten-1': '#ff526f', 'darken-1': '#c30040', 'darken-2': '#a4002a', 'darken-3': '#850017', 'darken-4': '#670000',
        });
        Object.assign(accentVariants, {
            'lighten-5': '#ffd8ff', 'lighten-4': '#ffbaed', 'lighten-3': '#ff9dd1', 'lighten-2': '#ff7fb6',
            'lighten-1': '#ff619b', 'darken-1': '#df1368', 'darken-2': '#c00050', 'darken-3': '#a1003a', 'darken-4': '#820025',
        });
    }
    return {
        dark: palette.dark,
        colors: {
            ...functionalColors,
            'primary': palette.primary,
            ...Object.fromEntries(Object.entries(primaryVariants).map(([key, value]) => [`primary-${key}`, value])),
            'primary-readable': palette.primary,
            'primary-readable-hover': palette.dark ? primaryVariants['lighten-1'] : primaryVariants['darken-1'],
            'secondary': palette.secondary,
            ...Object.fromEntries(Object.entries(secondaryVariants).map(([key, value]) => [`secondary-${key}`, value])),
            'secondary-readable': palette.dark ? secondaryVariants['lighten-1'] : secondaryVariants['darken-1'],
            'secondary-readable-hover': palette.dark ? secondaryVariants['lighten-2'] : secondaryVariants['darken-2'],
            'accent': palette.accent,
            ...Object.fromEntries(Object.entries(accentVariants).map(([key, value]) => [`accent-${key}`, value])),
            'success-readable': palette.dark ? functionalColors['success-lighten-1'] : functionalColors['success-darken-2'],
            'warning-readable': palette.dark ? functionalColors['warning-lighten-1'] : functionalColors['warning-darken-3'],
            'error-readable': palette.dark ? functionalColors['error-lighten-1'] : functionalColors['error-darken-2'],
            'info-readable': palette.dark ? functionalColors['info-lighten-1'] : functionalColors['info-darken-2'],
            'twitter-readable': palette.dark ? functionalColors['twitter-lighten-2'] : '#245fae',
            'twitter-readable-hover': palette.dark ? functionalColors['twitter-lighten-1'] : '#1d4f91',
            'jikkyo-festival-readable': palette.dark ? '#f06f84' : '#b51f3d',
            'jikkyo-so-many-readable': palette.dark ? '#f07d68' : '#ad3825',
            'jikkyo-many-readable': palette.dark ? functionalColors['jikkyo-many'] : '#895400',
            'surface': palette.vuetifySurface ?? palette.surface,
            'surface-bright': palette.surfaceBright ?? palette.elevatedHigh,
            'surface-variant': palette.surfaceBright ?? palette.elevatedHigh,
            'background': palette.background,
            'background-lighten-1': palette.surface,
            'background-lighten-2': palette.elevated,
            'background-lighten-3': palette.elevatedHigh,
            'text': palette.text,
            'text-darken-1': palette.textMuted,
            'text-darken-2': palette.textSubtle,
            'text-darken-3': palette.textDisabled,
            'gray': palette.gray,
            'black': palette.black,
            'navigation-active': palette.navigationActive,
            // 選択中ナビのアイコンは primary のまま保ち、文字だけを一段明暗調整して読みやすくする
            'navigation-active-text': palette.dark ? primaryVariants['lighten-1'] : primaryVariants['darken-1'],
            'player-overlay': palette.playerOverlay,
            'player-on-overlay': palette.playerOnOverlay,
            'on-background': palette.text,
            'on-surface': palette.text,
            'on-surface-bright': palette.text,
            'on-surface-variant': palette.text,
            'on-primary': onPrimary,
            'on-secondary': onSecondary,
            'on-accent': onAccent,
            // 機能色は明るい背景色なので、白文字ではなくテーマ固有の黒を使って可読性を確保する
            'on-success': palette.black,
            'on-warning': palette.black,
            'on-error': palette.black,
            'on-info': palette.black,
            'on-twitter': getReadableOnColor(functionalColors.twitter),
        },
        variables: {
            'hover-opacity': 0.08,
            'activated-opacity': 0.24,
        },
    };
};

const palettes: Record<KonomiTVBS4KTheme, IKonomiTVBS4KThemePalette> = {
    KonomiClassic: {
        dark: true,
        background: '#1e1310', surface: '#2f221f', elevated: '#433532', elevatedHigh: '#4c3c38',
        vuetifySurface: '#1e1310', surfaceBright: '#786968',
        primary: '#e7599d', secondary: '#e33157', accent: '#ff4081',
        text: '#ffeaea', textMuted: '#d9c7c7', textSubtle: '#aa9f9f', textDisabled: '#786968',
        gray: '#66514c', black: '#110a09', navigationActive: '#3a1d27',
        playerOverlay: '#2f221f', playerOnOverlay: '#ffeaea',
        legacyColorVariants: true,
    },
    KonomiNavy: {
        dark: true,
        background: '#111721', surface: '#1b2431', elevated: '#293646', elevatedHigh: '#354559',
        primary: '#f06aa8', secondary: '#d95682', accent: '#ff82b6',
        text: '#f3edf1', textMuted: '#c7c0c5', textSubtle: '#a19ca1', textDisabled: '#6d6970',
        gray: '#596577', black: '#090d13', navigationActive: '#3e2940',
        playerOverlay: '#1b2431', playerOnOverlay: '#f3edf1',
    },
    KonomiCharcoal: {
        dark: true,
        background: '#161616', surface: '#222222', elevated: '#303030', elevatedHigh: '#3b3b3b',
        primary: '#e95f9f', secondary: '#d34f77', accent: '#ff78ae',
        text: '#f3f0f1', textMuted: '#c5c0c2', textSubtle: '#9b9798', textDisabled: '#6f6b6d',
        gray: '#626062', black: '#0b0b0b', navigationActive: '#3b212c',
        playerOverlay: '#222222', playerOnOverlay: '#f3f0f1',
    },
    DeepPlum: {
        dark: true,
        background: '#1b141d', surface: '#2a1e2c', elevated: '#3a2a3d', elevatedHigh: '#49364d',
        primary: '#f174a8', secondary: '#d85d88', accent: '#ff8db8',
        text: '#f8edf3', textMuted: '#cdbfc7', textSubtle: '#a1949c', textDisabled: '#746a72',
        gray: '#69576a', black: '#0d090f', navigationActive: '#4c2941',
        playerOverlay: '#2a1e2c', playerOnOverlay: '#f8edf3',
    },
    NightBlue: {
        dark: true,
        background: '#101820', surface: '#192630', elevated: '#253744', elevatedHigh: '#314958',
        primary: '#5eb8e8', secondary: '#36a899', accent: '#72d0f4',
        text: '#eef4f7', textMuted: '#c0cbd1', textSubtle: '#91a0a8', textDisabled: '#657681',
        gray: '#526b79', black: '#080d11', navigationActive: '#193f55',
        playerOverlay: '#192630', playerOnOverlay: '#eef4f7',
    },
    DayBlue: {
        dark: false,
        background: '#eef6fa', surface: '#ffffff', elevated: '#dcebf2', elevatedHigh: '#c9dfe9',
        primary: '#1f6f9b', secondary: '#226f68', accent: '#087198',
        text: '#1d2d36', textMuted: '#4b606b', textSubtle: '#576b75', textDisabled: '#9aaab3',
        gray: '#b8cad3', black: '#0d171c', navigationActive: '#dff0f7',
        playerOverlay: '#ffffff', playerOnOverlay: '#1d2d36',
    },
    KonomiIvory: {
        dark: false,
        background: '#f7f3f5', surface: '#ffffff', elevated: '#eee7eb', elevatedHigh: '#e3d9de',
        primary: '#c23876', secondary: '#b83a5c', accent: '#d94d8d',
        text: '#292126', textMuted: '#574a50', textSubtle: '#72656c', textDisabled: '#a99da3',
        gray: '#c9bfc4', black: '#171215', navigationActive: '#faf0f5',
        playerOverlay: '#ffffff', playerOnOverlay: '#292126',
    },
    PearlBlue: {
        dark: false,
        background: '#f2f6fa', surface: '#ffffff', elevated: '#e5ecf3', elevatedHigh: '#d7e2ec',
        primary: '#b83f78', secondary: '#486f91', accent: '#d4518c',
        text: '#232a30', textMuted: '#52616d', textSubtle: '#5e6b75', textDisabled: '#9cabb6',
        gray: '#becbd5', black: '#12171b', navigationActive: '#e8f1f8',
        playerOverlay: '#ffffff', playerOnOverlay: '#232a30',
    },
    WarmCream: {
        dark: false,
        background: '#faf6f0', surface: '#fffdfc', elevated: '#f1e8de', elevatedHigh: '#e7d9cb',
        primary: '#c23f70', secondary: '#b45a45', accent: '#d95e8e',
        text: '#2d2520', textMuted: '#655a52', textSubtle: '#72665e', textDisabled: '#ab9d91',
        gray: '#cec1b5', black: '#18130f', navigationActive: '#fbf4f0',
        playerOverlay: '#fffdfc', playerOnOverlay: '#2d2520',
    },
    CoolGray: {
        dark: false,
        background: '#f2f3f5', surface: '#ffffff', elevated: '#e3e5e8', elevatedHigh: '#d5d8dc',
        primary: '#be3d78', secondary: '#a64264', accent: '#d7518c',
        text: '#252629', textMuted: '#595c62', textSubtle: '#63666c', textDisabled: '#a3a7ad',
        gray: '#c4c7cc', black: '#131416', navigationActive: '#f6f1f3',
        playerOverlay: '#ffffff', playerOnOverlay: '#252629',
    },
};

export const KONOMITV_BS4K_THEMES: Record<KonomiTVBS4KTheme, ThemeDefinition> = Object.fromEntries(
    Object.entries(palettes).map(([name, palette]) => [name, createTheme(palette)]),
) as Record<KonomiTVBS4KTheme, ThemeDefinition>;

const themeOptionNames: {title: string; value: KonomiTVBS4KTheme}[] = [
    {title: 'Konomi Classic', value: 'KonomiClassic'},
    {title: 'Konomi Navy', value: 'KonomiNavy'},
    {title: 'Konomi Charcoal', value: 'KonomiCharcoal'},
    {title: 'Deep Plum', value: 'DeepPlum'},
    {title: 'Night Blue', value: 'NightBlue'},
    {title: 'Day Blue', value: 'DayBlue'},
    {title: 'Konomi Ivory', value: 'KonomiIvory'},
    {title: 'Pearl Blue', value: 'PearlBlue'},
    {title: 'Warm Cream', value: 'WarmCream'},
    {title: 'Cool Gray', value: 'CoolGray'},
];

export const KONOMITV_BS4K_THEME_OPTIONS: IKonomiTVBS4KThemeOption[] = themeOptionNames.map(option => ({
    ...option,
    dark: palettes[option.value].dark,
    preview: {
        background: palettes[option.value].background,
        surface: palettes[option.value].surface,
        elevated: palettes[option.value].elevated,
        primary: palettes[option.value].primary,
        accent: palettes[option.value].accent,
        text: palettes[option.value].text,
        textMuted: palettes[option.value].textMuted,
    },
}));

export const isKonomiTVBS4KTheme = (value: unknown): value is KonomiTVBS4KTheme => {
    return typeof value === 'string' && Object.prototype.hasOwnProperty.call(KONOMITV_BS4K_THEMES, value);
};
