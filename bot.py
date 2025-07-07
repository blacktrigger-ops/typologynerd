import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List

import discord
from discord import ui
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel
from beanie import Document, Indexed, init_beanie
from pydantic import Field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Database Model ---
class TypologyDefinition(Document):
    term: str = Indexed(str)
    text: str
    author_id: int
    author_name: str
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")], name="term_text_search"),
            IndexModel([("author_id", 1)], name="author_lookup"),
            IndexModel([("votes", -1)], name="popular_definitions")
        ]

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Database Connection ---
async def init_db():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    await init_beanie(
        database=client[os.getenv("MONGO_DB", "typology_bot")],
        document_models=[TypologyDefinition]
    )

# --- Pagination View ---
class DefinitionView(ui.View):
    def __init__(self, definitions: List[TypologyDefinition], term: str):
        super().__init__(timeout=120.0)
        self.definitions = definitions
        self.term = term
        self.page = 0
        self.per_page = 5
        self.message = None
        self.cooldown = commands.CooldownMapping.from_cooldown(1, 60.0, commands.BucketType.user)

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        start = self.page * self.per_page
        page_defs = self.definitions[start:start+self.per_page]
        
        embed = discord.Embed(
            title=f"📚 {self.term} Definitions (Page {self.page + 1})",
            color=0x6A0DAD
        )
        
        for idx, defn in enumerate(page_defs, start=1):
            embed.add_field(
                name=f"#{start + idx} (⭐ {defn.votes})",
                value=(
                    f"{defn.text}\n\n"
                    f"↳ *By {defn.author_name}*\n"
                    f"↳ *Reference: {defn.reference or 'None'}*\n"
                    f"↳ *Updated {discord.utils.format_dt(defn.last_updated, style='R')}*"
                ),
                inline=False
            )
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    @ui.button(emoji="⬅️", style=discord.ButtonStyle.blurple)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        await self.update_embed(interaction)

    @ui.button(emoji="➡️", style=discord.ButtonStyle.blurple)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        if (self.page + 1) * self.per_page < len(self.definitions):
            self.page += 1
            await self.update_embed(interaction)

    @ui.button(emoji="⭐", style=discord.ButtonStyle.green)
    async def upvote(self, interaction: discord.Interaction, button: ui.Button):
        definition_idx = self.page * self.per_page
        if definition_idx >= len(self.definitions):
            return
            
        definition = self.definitions[definition_idx]
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message(
                "You've already voted for this definition!",
                ephemeral=True
            )
            return
            
        definition.voters.append(interaction.user.id)
        definition.votes += 1
        definition.last_updated = datetime.utcnow()
        await definition.save()
        
        await interaction.response.send_message(
            "Vote recorded! ⭐",
            ephemeral=True
        )
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

# --- Bot Events ---
@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    # Process definition categorization
    if message.reference and message.content.startswith("tp define"):
        await process_definition(message)

# --- Command Implementation ---
async def process_definition(message):
    try:
        original = await message.channel.fetch_message(message.reference.message_id)
        
        parts = re.split(r'\s+', message.content, maxsplit=3)
        term = parts[2].upper()
        
        author = message.author
        if len(parts) > 3 and message.mentions:
            author = message.mentions[0]
        
        reference = ""
        if len(parts) > 3:
            reference = message.content.split(maxsplit=3)[3]
            if message.mentions:
                reference = reference.replace(f"<@{message.mentions[0].id}>", "").strip()
        
        definition = TypologyDefinition(
            term=term,
            text=original.content,
            author_id=author.id,
            author_name=str(author),
            reference=reference
        )
        await definition.insert()
        
        embed = discord.Embed(
            title="✅ Definition Saved",
            description=f"Term: **{term}**",
            color=0x00ff00
        )
        embed.add_field(name="Definition", value=original.content, inline=False)
        embed.add_field(name="Author", value=author.display_name)
        if reference:
            embed.add_field(name="Reference", value=reference)
        embed.set_footer(text=f"Added by {message.author}")
        
        await message.reply(embed=embed)
        
    except Exception as e:
        await message.reply(f"❌ Error: {str(e)}")

@bot.command()
async def define(ctx, term: str):
    """View definitions with pagination and voting"""
    definitions = await TypologyDefinition.find(
        TypologyDefinition.term == term.upper()
    ).sort("-votes").to_list()
    
    if not definitions:
        await ctx.send(f"No definitions found for **{term}**")
        return
    
    view = DefinitionView(definitions, term.upper())
    view.message = await ctx.send(
        embed=discord.Embed(
            title=f"Loading {term.upper()} definitions...",
            color=0x6A0DAD
        ),
        view=view
    )
    await view.update_embed()

@bot.command()
async def delete(ctx, definition_number: int):
    """Delete one of your definitions"""
    definitions = await TypologyDefinition.find(
        TypologyDefinition.author_id == ctx.author.id
    ).sort("-created_at").to_list()
    
    if not definitions or definition_number < 1 or definition_number > len(definitions):
        await ctx.send("Invalid definition number or no definitions found")
        return
    
    definition = definitions[definition_number - 1]
    await definition.delete()
    
    await ctx.send(f"✅ Definition #{definition_number} deleted")

@bot.command()
async def edit(ctx, definition_number: int):
    """Edit one of your definitions"""
    # Cooldown check
    bucket = ctx.bot.get_cog('EditCooldown').cooldown.get_bucket(ctx.message)
    retry_after = bucket.update_rate_limit()
    if retry_after:
        await ctx.send(f"⏳ Please wait {retry_after:.1f} seconds before editing again")
        return
    
    definitions = await TypologyDefinition.find(
        TypologyDefinition.author_id == ctx.author.id
    ).sort("-created_at").to_list()
    
    if not definitions or definition_number < 1 or definition_number > len(definitions):
        await ctx.send("Invalid definition number or no definitions found")
        return
    
    definition = definitions[definition_number - 1]
    prompt = await ctx.send(
        f"Reply to this message with your new text for definition #{definition_number}"
    )
    
    def check(m):
        return (
            m.author == ctx.author and
            m.reference and
            m.reference.message_id == prompt.id
        )
    
    try:
        reply = await bot.wait_for('message', check=check, timeout=120.0)
        definition.text = reply.content
        definition.last_updated = datetime.utcnow()
        await definition.save()
        await ctx.send("✅ Definition updated successfully!")
    except asyncio.TimeoutError:
        await ctx.send("⌛ Edit timed out")

# --- Cooldown Setup ---
class EditCooldown(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cooldown = commands.CooldownMapping.from_cooldown(1, 60.0, commands.BucketType.user)

async def setup(bot):
    await bot.add_cog(EditCooldown(bot))

# --- Run Bot ---
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        print(f"Missing environment variables: {', '.join(missing_vars)}")
        exit(1)
    
    asyncio.run(setup(bot))
    bot.run(os.getenv("DISCORD_TOKEN"))
