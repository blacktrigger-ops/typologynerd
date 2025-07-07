import os
import asyncio
from datetime import datetime
from typing import List, Optional
import discord
from discord import ui
from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import IndexModel
from beanie import Document, init_beanie
from pydantic import Field
from dotenv import load_dotenv
from pymongo import TEXT
load_dotenv()

# ======================
# DATABASE MODEL
# ======================
class TypologyDefinition(Document):
    term: str
    text: str
    author_id: int
    author_name: str
    categorizer_id: int
    reference: str = Field(default="")
    votes: int = Field(default=0)
    voters: List[int] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    
    class Settings:
        name = "typology_definitions"
        indexes = [
            IndexModel([("term", "text")], name="term_text_idx"),
            IndexModel([("term", -1), ("votes", -1)], name="popularity_idx"),
            IndexModel([("author_id", 1)], name="author_idx"),
            IndexModel([("categorizer_id", 1),
IndexModel([("term", TEXT), ("text", TEXT)], name="term_text_search")], name="categorizer_idx")
        ]

# ======================
# DATABASE INITIALIZATION
# ======================

async def initialize_database():
    client = AsyncIOMotorClient(os.getenv("MONGO_URI"))
    db = client[os.getenv("MONGO_DB", "typology_bot")]
    
    # Initialize Beanie without auto-indexing
    await init_beanie(
        database=db,
        document_models=[TypologyDefinition],
        allow_index_dropping=False,
        create_indexes=False  # CRITICAL FIX: Disable auto-index creation
    )
    
    # Manual index management to prevent conflicts
    collection = db["typology_definitions"]
    existing_indexes = await collection.index_information()
    
    for index in TypologyDefinition.Settings.indexes:
        index_name = index.document["name"]
        index_keys = tuple(index.document["key"])
        
        # Check for existing index with same keys but different name
        conflict_exists = False
        for existing_name, existing_info in existing_indexes.items():
            if existing_name == "_id_":
                continue
                
            # Handle both list and tuple representations
            existing_keys = tuple(existing_info["key"])
            if isinstance(existing_keys[0], tuple):
                existing_keys = tuple((k, v) for k, v in existing_keys)
            else:
                existing_keys = tuple(existing_keys)
                
            # Compare normalized key sets
            if set(existing_keys) == set(index_keys) and existing_name != index_name:
                conflict_exists = True
                try:
                    await collection.drop_index(existing_name)
                    print(f"♻️ Dropped conflicting index: {existing_name}")
                    break  # Only need to handle one conflict per index
                except Exception as e:
                    print(f"⚠️ Failed to drop index {existing_name}: {str(e)}")
        
        # Create index if needed (handle both new and conflict cases)
        if conflict_exists or index_name not in existing_indexes:
            try:
                await collection.create_indexes([index])
                print(f"✅ Created index: {index_name}")
            except Exception as e:
                print(f"⚠️ Failed to create index {index_name}: {str(e)}")
# ======================
# INTERACTIVE UI
# ======================
class DefinitionView(ui.View):
    def __init__(self, definitions: List[TypologyDefinition], user_id: int):
        super().__init__(timeout=120.0)
        self.definitions = definitions
        self.user_id = user_id
        self.page = 0
        self.per_page = 5
        self.message = None

    async def update_embed(self, interaction: Optional[discord.Interaction] = None):
        start = self.page * self.per_page
        page_defs = self.definitions[start:start+self.per_page]
        
        embed = discord.Embed(
            title=f"📖 {self.definitions[0].term} Definitions (Page {self.page + 1})",
            color=0x6A0DAD,
            timestamp=datetime.utcnow()
        )
        
        for idx, defn in enumerate(page_defs, 1):
            embed.add_field(
                name=f"#{start + idx} (⭐ {defn.votes})",
                value=(
                    f"{defn.text}\n\n"
                    f"↳ By {defn.author_name}\n"
                    f"↳ Categorized by {await self.fetch_username(defn.categorizer_id)}\n"
                    f"↳ Reference: {defn.reference or 'None'}\n"
                    f"↳ Updated {discord.utils.format_dt(defn.last_updated, style='R')}"
                ),
                inline=False
            )
        
        if interaction:
            await interaction.response.edit_message(embed=embed, view=self)
        elif self.message:
            await self.message.edit(embed=embed, view=self)

    async def fetch_username(self, user_id: int) -> str:
        try:
            user = await bot.fetch_user(user_id)
            return user.display_name
        except:
            return "Unknown User"

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
        definition = self.definitions[self.page * self.per_page]
        
        if interaction.user.id in definition.voters:
            await interaction.response.send_message("❌ You've already voted!", ephemeral=True)
            return
            
        definition.voters.append(interaction.user.id)
        definition.votes += 1
        definition.last_updated = datetime.utcnow()
        await definition.save()
        
        await interaction.response.send_message("✅ Vote recorded!", ephemeral=True)
        await self.update_embed()

    @ui.button(emoji="✏️", style=discord.ButtonStyle.gray)
    async def edit_btn(self, interaction: discord.Interaction, button: ui.Button):
        definition = self.definitions[self.page * self.per_page]
        
        if interaction.user.id != definition.author_id:
            await interaction.response.send_message(
                "❌ You can only edit your own definitions", 
                ephemeral=True
            )
            return
            
        modal = EditModal(definition)
        await interaction.response.send_modal(modal)

    @ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def delete_btn(self, interaction: discord.Interaction, button: ui.Button):
        definition = self.definitions[self.page * self.per_page]
        
        if interaction.user.id != definition.author_id:
            await interaction.response.send_message(
                "❌ You can only delete your own definitions", 
                ephemeral=True
            )
            return
            
        await definition.delete()
        self.definitions.pop(self.page * self.per_page)
        
        if not self.definitions:
            await interaction.response.edit_message(
                content=f"✅ All definitions for {definition.term} deleted",
                embed=None,
                view=None
            )
            return
            
        await interaction.response.send_message("✅ Definition deleted", ephemeral=True)
        await self.update_embed()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            await self.message.edit(view=self)

class EditModal(ui.Modal, title="Edit Definition"):
    new_text = ui.TextInput(label="New Definition Text", style=discord.TextStyle.long)
    
    def __init__(self, definition: TypologyDefinition):
        super().__init__()
        self.definition = definition
        self.new_text.default = definition.text
    
    async def on_submit(self, interaction: discord.Interaction):
        self.definition.text = str(self.new_text)
        self.definition.last_updated = datetime.utcnow()
        await self.definition.save()
        await interaction.response.send_message("✅ Definition updated!", ephemeral=True)

# ======================
# BOT SETUP
# ======================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Database initialization attempt {attempt}/3")
            await initialize_database()
            print(f"✅ Bot ready as {bot.user}")
            break
        except Exception as e:
            print(f"❌ Attempt {attempt} failed: {str(e)}")
            if attempt == 3:
                print("💥 Failed to initialize database after 3 attempts")
                raise
            await asyncio.sleep(2 ** attempt)

# ======================
# MESSAGE HANDLING
# ======================
@bot.event
async def on_message(message):
    await bot.process_commands(message)
    
    if (
        message.reference and
        message.content.lower().startswith("tp define") and
        not message.author.bot
    ):
        try:
            # Fetch original message
            original = await message.channel.fetch_message(message.reference.message_id)
            if original.author.bot:
                return
                
            # Parse command
            parts = message.content.split(maxsplit=3)
            if len(parts) < 3:
                await message.channel.send("❌ Format: `tp define TERM @author [reference]`")
                return
                
            term = parts[2].upper()
            author = message.mentions[0] if message.mentions else original.author
            reference = parts[3].split("@")[0].strip() if len(parts) > 3 else ""
            
            # Check for duplicates
            exists = await TypologyDefinition.find(
                TypologyDefinition.term == term,
                TypologyDefinition.text == original.content,
                TypologyDefinition.author_id == author.id
            ).first_or_none()
            
            if exists:
                await message.channel.send("⚠️ This definition already exists!")
                return
                
            # Create definition
            definition = TypologyDefinition(
                term=term,
                text=original.content,
                author_id=author.id,
                author_name=str(author),
                categorizer_id=message.author.id,
                reference=reference,
                voters=[author.id, message.author.id]
            )
            await definition.insert()
            
            # Send confirmation
            embed = discord.Embed(
                title=f"✅ Definition saved for {term}",
                description=original.content,
                color=0x00ff00
            )
            embed.add_field(name="Author", value=author.display_name)
            embed.add_field(name="Categorized by", value=message.author.display_name)
            if reference:
                embed.add_field(name="Reference", value=reference)
                
            await message.channel.send(embed=embed)
            
        except Exception as e:
            await message.channel.send(f"❌ Error: {str(e)}")

# ======================
# COMMANDS
# ======================
@bot.command()
async def define(ctx, term: str):
    """View definitions with interactive controls"""
    try:
        definitions = await TypologyDefinition.find(
            TypologyDefinition.term == term.upper()
        ).sort(-TypologyDefinition.votes).to_list()
        
        if not definitions:
            await ctx.send(f"❌ No definitions found for {term.upper()}")
            return
            
        view = DefinitionView(definitions, ctx.author.id)
        view.message = await ctx.send(
            embed=discord.Embed(
                title=f"Loading {term.upper()} definitions...",
                color=0x6A0DAD
            ),
            view=view
        )
        await view.update_embed()
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command()
async def search(ctx, *, query: str):
    """Search across all definitions"""
    try:
        results = await TypologyDefinition.find(
            {"$text": {"$search": query}}
        ).sort(-TypologyDefinition.votes).limit(5).to_list()
        
        if not results:
            await ctx.send("🔍 No results found")
            return
            
        embed = discord.Embed(title="🔍 Search Results", color=0x3498DB)
        for result in results:
            embed.add_field(
                name=f"{result.term} (⭐ {result.votes})",
                value=f"{result.text[:150]}...",
                inline=False
            )
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Search error: {str(e)}")

# ======================
# START BOT
# ======================
if __name__ == "__main__":
    required_vars = ["DISCORD_TOKEN", "MONGO_URI"]
    if missing := [var for var in required_vars if not os.getenv(var)]:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        exit(1)
    
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except Exception as e:
        print(f"💥 Bot crashed: {str(e)}")